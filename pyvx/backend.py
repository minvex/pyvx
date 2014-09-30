from pyvx.types import *
from cffi import FFI
from pycparser import c_parser, c_ast
from pycparser.c_generator import CGenerator
from collections import defaultdict

class Context(object):
    pass

class Image(object):
    count = 0

    def __init__(self, width=0, height=0, color=FOURCC_VIRT,
                 data=None, context=None, virtual=None, graph=None):
        if graph is None:
            graph = Graph.current_graph
        if context is None:
            context = graph.context
        if virtual is None:
            virtual = (color == FOURCC_VIRT)
        if virtual:
            assert graph is not None
        self.context = context
        self.width = width
        self.height = height
        self.color = color
        self.virtual = virtual
        self.graph = graph
        self.producer = None
        if data is not None:
            self.set_data_pointer(data)
        else:
            self.data = None
        Image.count += 1
        self.count = Image.count

    def set_data_pointer(self, data):
        if hasattr(data, 'typecode'):
            assert data.typecode == self.color.dtype
        if hasattr(data, 'to_cffi'):
            self.data = data.to_cffi(FFI())
        elif hasattr(data, 'buffer_info'):
            addr, l = data.buffer_info()
            assert l == self.width * self.height * self.color.items
            self.data = FFI().cast(self.color.base_type + ' *', addr)
        else:
            raise NotImplementedError("Dont know how to convert %r to a cffi buffer" % data)
        self._keep_alive_original_data = data

    def force(self, data=None):
        if data is not None:
            self.set_data_pointer(data)
        self.virtual = False

    def ensure_shape(self, width_or_image, height=None):
        if height is None:
            width, height = width_or_image.width, width_or_image.height
        else:
            width = width_or_image
        if self.width == 0:
            self.width = width
        if self.height == 0:
            self.height = height
        if self.width != width or self.height != height:
            raise InvalidFormatError

    def ensure_color(self, color):
        self.suggest_color(color)
        if  self.color != color:
            raise InvalidFormatError            

    def suggest_color(self, color):
        if self.color == FOURCC_VIRT:
            self.color = color

    def ensure_similar(self, image):
        self.ensure_shape(image)
        self.suggest_color(image.color)
        if self.color.items != image.color.items:
            raise InvalidFormatError

    def alloc(self):
        if self.data is None:
            items = self.width * self.height * self.color.items
            self.data = FFI().new(self.color.base_type + '[]', items)
        addr = FFI().cast('long', self.data)
        self.ctype = self.color.base_type + " *"
        self.csym = "__img%d" % self.count
        self.cdeclaration = "%s __restrict__ %s = ((%s) 0x%x);\n" % (
                self.ctype, self.csym, self.ctype, addr)

    def getitem2d(self, node, x, y):
        name = self.csym
        if node.border_mode == BORDER_MODE_UNDEFINED:
            l = self.width * self.height - 1
            return "%s[clamp((%s) * %d + (%s), 0, %d)]" % (name, y, self.width, x, l)
        elif node.border_mode == BORDER_MODE_REPLICATE:
            return "%s[clamp(%s, 0, %d) * %d + clamp(%s, 0, %d)]" % (
                   name, y, self.height-1, self.width, x, self.width-1)
        else:
            raise NotImplementedError

    def getitem1d(self, node, idx):
        name = self.csym
        if node.border_mode == BORDER_MODE_UNDEFINED:
            l = self.width * self.height - 1
            return "%s[clamp(%s, 0, %d)]" % (name, idx, l)
        else:
            raise NotImplementedError

    def getattr(self, node, attr):
        name = self.csym
        if attr == "width":
            return str(self.width)
        elif attr == "height":
            return str(self.height)
        elif attr == "data":
            return name
        elif attr == "len":
            return str(self.width * self.height)
        else:
            raise AttributeError

    def __add__(self, other):
        return Add(self, other, CONVERT_POLICY_TRUNCATE)


class Graph(object):
    default_context = None
    current_graph = None

    def __init__(self, context=None, early_verify=True):
        if context is None:
            if Graph.default_context is None:
                Graph.default_context = Context()
            context = Graph.default_context
        self.context = context
        self.nodes = []
        self.data_objects = set()
        self.early_verify = early_verify

    def __enter__(self):
        assert Graph.current_graph is None
        Graph.current_graph = self

    def __exit__(self, *args):
        assert Graph.current_graph is self
        Graph.current_graph = None

    def _add_node(self, node):
        self.nodes.append(node)
        for d in node.inputs + node.outputs + node.inouts:
            self.data_objects.add(d)

    def verify(self):
        self.images = [d for d in self.data_objects if isinstance(d, Image)]
        self.nodes = self.schedule()

        for node in self.nodes:
            node.do_verify()

        # Virtual data produced
        for d in self.images:
            if d.virtual and d.producer is None:
                raise InvalidGraphError("Virtual data never produced.")
            if d.color == FOURCC_VIRT:
                raise InvalidFormatError("FOURCC_VIRT not resolved into specific type.")

        self.compile()

    def schedule(self):
        present = defaultdict(lambda : True)
        for d in self.images:
            present[d] = not d.virtual
        for n in self.nodes:
            for d in n.outputs + n.inouts:
                present[d] = False
        worklist = self.nodes[:]
        inorder = []
        while worklist:
            remaining = []
            for n in worklist:
                if all(present[d] for d in n.inputs):
                    inorder.append(n)
                    for d in n.outputs:
                        present[d] = True
                else:
                    remaining.append(n)
            if len(worklist) == len(remaining):
                raise InvalidGraphError("Loops not allowed in the graph.")
            worklist = remaining
        return inorder

    def compile(self):
        for d in self.images:
            d.alloc()
        imgs = ''.join(d.cdeclaration for d in self.images)
        code = Code('''
                    long clamp(long val, long min_val, long max_val) {
                        if (val < min_val) return min_val;
                        if (val > max_val) return max_val;
                        return val;
                    }
                    \n''' + imgs)
        for n in self.nodes:
            n.compile(code)
        ffi = FFI()
        ffi.cdef("void func(void);")
        #print str(code)
        lib = ffi.verify("void func(void) {" + str(code) + "}",
                         extra_compile_args=["-O3", "-march=native", "-std=c99"])
        self.compiled_func = lib.func

    def process(self):
        self.compiled_func()

def cparse(code):
    parser = c_parser.CParser()
    ast = parser.parse("void f() {" + code + "}")
    func = ast.ext[0]
    assert func.decl.name == 'f'
    return func.body

class MagicCGenerator(CGenerator):
    def __init__(self, cxnode, magic_vars):
        CGenerator.__init__(self)
        self.cxnode = cxnode
        self.magic_vars = magic_vars

    def visit_StructRef(self, node):
        assert isinstance(node.name, c_ast.ID)
        assert isinstance(node.field, c_ast.ID)
        if node.name.name in self.magic_vars:
            var = self.magic_vars[node.name.name]
            return var.getattr(self.cxnode, node.field.name)
        return CGenerator.visit_StructRef(self, node)

    def visit_ArrayRef(self, node):
        if not isinstance(node.name, c_ast.ID):
            return CGenerator.visit_ArrayRef(self, node)
        if isinstance(node.subscript, c_ast.ExprList):
            if node.name.name in self.magic_vars:
                x, y = node.subscript.exprs
                var = self.magic_vars[node.name.name]
                return var.getitem2d(self.cxnode, self.visit(x), self.visit(y))
        else:
            if node.name.name in self.magic_vars:
                var = self.magic_vars[node.name.name]
                return var.getitem1d(self.cxnode, self.visit(node.subscript))                
        return CGenerator.visit_ArrayRef(self, node)

class Code(object):
    
    def __init__(self, code=''):
        self.code = code

    def add_block(self, cxnode, code, **magic_vars):
        ast = cparse(code)
        #ast.show()
        generator = MagicCGenerator(cxnode, magic_vars)
        self.code += generator.visit(ast)

    def __str__(self):
        return self.code

class Node(object):

    def __init__(self, graph, *args, **kwargs):
        self.graph = graph
        self.inputs, self.outputs, self.inouts = [], [], []
        for i, v in enumerate(self.signature.split(',')):
            v = v.strip().split(' ')
            direction, name = v[0], v[-1]
            if i < len(args):
                val = args[i]
                if name in kwargs:
                    raise TypeError("Got multiple values for keyword argument '%s'" % name)
            elif name in kwargs:
                val = kwargs[name]
            else:
                raise TypeError("Required argument missing")
            if direction == 'in':
                self.inputs.append(val)
            elif direction == 'out':
                self.outputs.append(val)
            elif direction == 'inout':
                self.inouts.append(val)
            else:
                raise TypeError("Bad direction '%s' of argument '%s'" % (direction, name))
            setattr(self, name, val)
        self.setup()

    def setup(self):
        self.graph._add_node(self)
        self.border_mode = BORDER_MODE_UNDEFINED
        self.border_mode_value = 0
        for d in self.outputs + self.inouts:
            if d.producer is None:
                d.producer = self
        if self.graph.early_verify:
            self.do_verify()

    def do_verify(self):
        # Signle writer
        for d in self.outputs + self.inouts:
            if d.producer is not self:
                raise MultipleWritersError

        # Bidirection data not virtual
        for d in self.inouts:
            if d.virtual:
                raise InvalidGraphError("Bidirection data cant be virtual.")

        for d in self.inputs:
            if isinstance(d, Image) and (not d.width or not d.height):
                raise InvalidFormatError
        self.verify()

    def ensure(self, condition):
        if not condition:
            raise InvalidFormatError


class AddNode(Node):
    signature = "in in1, in in2, in policy, out out"

    def verify(self):
        if self.policy != CONVERT_POLICY_TRUNCATE:
            raise NotImplementedError
        t = binop_type(self.in1.color, self.in2.color)
        self.out.suggest_color(t)
        self.out.ensure_similar(self.in1)
        self.out.ensure_similar(self.in2)

    def compile(self, code):
        code.add_block(self, """
            for (long i = 0; i < out.len; i++) {
                out[i] = in1[i] + in2[i];
            }
            """, in1=self.in1, in2=self.in2, out=self.out)

def Add(in1, in2, policy):
    res = Image()
    AddNode(Graph.current_graph, in1, in2, CONVERT_POLICY_TRUNCATE, res)
    return res



class ChannelExtractNode(Node):
    signature = "in input, in channel, out output"

    def verify(self):
        if self.input.color == FOURCC_UYVY and self.channel == CHANNEL_Y:
            pass
        else:
            raise InvalidFormatError(
                'Cant extract channel %s from %s image.' % (self.channel,
                                                            self.input.color))
        self.output.ensure_similar(self.input)

def ChannelExtract(input, channel):
    output = Image()
    ChannelExtractNode(Graph.current_graph,
                          input, channel, output)
    return output


class Gaussian3x3Node(Node):
    signature = "in input, out output"

    def verify(self):
        self.ensure(self.input.color.items == 1)
        self.output.ensure_similar(self.input)

    def compile(self, code):
        code.add_block(self, """
            for (long y = 0; y < img.height; y++) {
                for (long x = 0; x < img.width; x++) {
                    res[x, y] = (1*img[x-1, y-1] + 2*img[x, y-1] + 1*img[x+1, y-1] +
                                 2*img[x-1, y]   + 4*img[x, y]   + 2*img[x+1, y]   +
                                 1*img[x-1, y+1] + 2*img[x, y+1] + 1*img[x+1, y+1]) / 16;
                }
            }
            """, img=self.input, res=self.output)

def Gaussian3x3(input):
    output = Image()
    Gaussian3x3Node(Graph.current_graph, input, output)
    return output


class Sobel3x3Node(Node):
    signature = 'in input, out output_x, out output_y'

    def verify(self):
        self.ensure(self.input.color.items == 1)
        self.output_x.ensure_similar(self.input)
        self.output_y.ensure_similar(self.input)

def Sobel3x3(input):
    dx, dy = Image(), Image()
    Sobel3x3Node(Graph.current_graph, input, dx, dy)
    return dx, dy


class MagnitudeNode(Node):
    signature = 'in grad_x, in grad_y, out mag'

    def verify(self):
        self.ensure(self.grad_x.color.items == 1)
        self.ensure(self.grad_y.color.items == 1)
        self.mag.ensure_similar(self.input)

def Magnitude(grad_x, grad_y):
    mag = Image()
    MagnitudeNode(Graph.current_graph, grad_x, grad_y, mag)
    return mag


class PhaseNode(Node):
    signature = 'in grad_x, in grad_y, out orientation'

    def verify(self):
        self.ensure(self.grad_x.color.items == 1)
        self.ensure(self.grad_y.color.items == 1)
        self.orientation.ensure_similar(self.input)

def Phase(grad_x, grad_y):
    ph = Image()
    PhaseNode(Graph.current_graph, grad_x, grad_y, ph)
    return ph


class AccumulateImageNode(Node):
    signature = 'in input, inout accum'

    def verify(self):
        pass

def AccumulateImage(input):
    accum = Image()
    AccumulateImageNode(Graph.current_graph, input, accum)
    return accum


if __name__ == '__main__':
    from imgpy.io import Mplayer, view
    video = Mplayer("/usr/share/cognimatics/data/facit/events/passanger/bustst1-M3014-180.mjpg", True)
    frame = video.next()
    w, h = frame.width, frame.height
    res = frame.new()    

    g = Graph()
    with g:
        img = Image(w, h, FOURCC_U8, data=frame)
        gimg = Gaussian3x3(img)
        gimg.producer.border_mode = BORDER_MODE_REPLICATE
        gimg.force(res)
        # dx, dy = Sobel3x3(gimg)
        # mag = Magnitude(dx, dy)
        # phi = Phase(dx, dy)
    g.verify()

    for new_frame in video:
        frame.data[:] = new_frame.data
        g.process()
        view(res)