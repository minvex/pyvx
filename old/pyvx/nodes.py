"""
:mod:`pyvx.nodes` --- Node implementations
==========================================

This module contains the implementations of the different processing nodes. They
are implemented by subclassing ``Node`` and overriding ``signature``, ``verify()``
and ``compile()``. As an example here is the implementation of the Gaussian3x3Node:

.. code-block:: python 

    class Gaussian3x3Node(Node):
        signature = "in image input, out image output"

        def verify(self):
            self.ensure(self.input.image_format.items == 1)
            self.output.ensure_similar(self.input)

        def compile(self, code):
            code.add_block(self, \"""
                for (long y = 0; y < img.height; y++) {
                    for (long x = 0; x < img.width; x++) {
                        res[x, y] = (1*img[x-1, y-1] + 2*img[x, y-1] + 1*img[x+1, y-1] +
                                     2*img[x-1, y]   + 4*img[x, y]   + 2*img[x+1, y]   +
                                     1*img[x-1, y+1] + 2*img[x, y+1] + 1*img[x+1, y+1]) / 16;
                    }
                }
                \""", img=self.input, res=self.output)

- ``Node.signature`` is a string specifying the argument names and there directions 
  (in, out or inout). The arguments will be assigned to attributes with the same
  names when the node is created. The arguments can be given default values by 
  assigning them to class-level attributes.

- ``Node.verify(self)`` is called during the verification phase and can assume that all 
  nodes it depend on have verified successfully. It is supposed to check the
  arguments and raise one of the ``VerificationError``'s if they don't make sense. Also
  any output images with width/height set to 0 or color set to DF_IMAGE_VIRT should be given 
  proper values. There are a few helper methods available described below.

- ``Node.compile(self, code)`` is called after verification of the entire graph was
  successful. It is responsible for generating C code implementing the node using
  the ``code`` argument. It has a notion of magic variables used to abstract away 
  the pixel access calculations. See :class:`pyvx.codegen.Code`. 

To simplify the implementation of ``verify()`` there are a few helper functions. They
will updated the properties of the images if they've not yet been set, and 
raise ``ERROR_INVALID_FORMAT`` if they were set to something different.

- ``Image.ensure_shape(self, other_image)`` Ensures ``self`` has the same width
  and height as other_image.

- ``Image.ensure_shape(self, width, height)`` Ensures ``self`` has the width 
  ``width`` an the height ``height``.

- ``Image.ensure_color(self, color)`` Ensures that the color of ``self`` is ``color``

- ``Image.suggest_color(self, color)`` Sets ``self.color`` to ``color`` if it is not 
  yet specified.

- ``Image.ensure_similar(self, image)`` Ensures that the shape and number of channels
  of ``self`` and ``image`` are the same, and suggests that the color of ``self`` is 
  the same as ``image``.

- ``Node.ensure(self, condition)`` raises ``ERROR_INVALID_FORMAT`` if ``condition``
  is ``false``.

To allow for a general implementation of the graph optimizations, there are special 
subclasses of ``Node`` that should be used. For strictly element-wise operations
there is ``ElementwiseNode``. It expects a ``body`` attribute with the code
that will be executed for each pixel. This code can assume that there are variables
with the same name as the arguments available. For the input arguments those 
variables contains the pixel values of the current pixel and it is the responsibility
of this code to assign the output variables. As an example, here is the implementation
of the ``PowerNode``:

.. code-block:: python 

    class PowerNode(ElementwiseNode):
        signature = "in image in1, in image in2, in enum convert_policy, out image out"
        body = "out = pow(in1, in2);"

If some logic is needed to produce the code, ``body`` can be implemented as a 
``property``. That is for example done by the ``BinaryOperationNode``:

.. code-block:: python 

    class BinaryOperationNode(ElementwiseNode):
        signature = "in image in1, in image op, in image in2, in enum convert_policy, out image out"
        @property
        def body(self):
            return "out = in1 %s in2;" % self.op

The default ``ElementwiseNode.verify()`` will ensure that the output images are of the same size
as the input images and will use :func:`pyvx.types.result_color`  to 
replace  ``DF_IMAGE_VIRT`` colors among the output images. If this is not appropriate 
it can be overridden.

"""
from pyvx.backend import *
import cffi
import os


class ElementwiseNode(Node):
    small_ints = ('uint8_t', 'int8_t', 'uint16_t', 'int16_t')

    def verify(self):
        inputs = self.input_images
        outputs = self.output_images + self.inout_images
        color = result_color(*[i.image_format for i in inputs])
        for img in outputs:
            img.suggest_color(color)
            img.ensure_similar(inputs[0])
            if self.convert_policy == CONVERT_POLICY_SATURATE:
                if img.image_format.ctype not in self.small_ints:
                    raise InvalidFormatError(
                        "Saturated arithmetic only supported for 8- and 16- bit integers.")

    def tmptype(self, ctype):
        if ctype in self.small_ints:
            return 'long'
        return ctype

    def compile(self, code, noloop=False):
        iin = [p.name for p in self.parameters 
                      if p.direction != OUTPUT and p.data_type == TYPE_IMAGE]
        iout = [p.name for p in self.parameters 
                      if p.direction != INPUT and p.data_type == TYPE_IMAGE]
        magic = {'__tmp_image_%s' % name: getattr(self, name) for name in iin + iout}
        setup = ''.join("%s %s;" % (self.tmptype(getattr(self, name).image_format.ctype), name)
                        for name in iin + iout)
        inp = ''.join("%s = __tmp_image_%s[__i];" % (name, name) for name in iin)
        outp = ''.join("__tmp_image_%s[__i] = %s;" % (name, name) for name in iout)
        if noloop:
            head = ""
        else:
            head = "for (long __i = 0; __i < __tmp_image_%s.values; __i++) " % iin[0]
        body = inp + self.body + outp
        block = head + "{" + body + "}"
        code.add_block(self, setup + block, **magic)


class MergedElementwiseNode(MergedNode):

    def compile(self, code):
        img = self.original_nodes[0].input_images[0]
        code.add_code("\n// MergedElementwiseNode\n")
        code.indent_level += 4
        code.add_code("for (long __i = 0; __i < %s; __i++) {\n" %
                      img.getattr(self, "values"))
        for n in self.original_nodes:
            n.compile(code, True)
        code.indent_level -= 4
        code.add_code("}\n")


class BinaryOperationNode(ElementwiseNode):
    signature = (param('in1', INPUT, TYPE_IMAGE),
                 param('op',  INPUT, TYPE_STRING),
                 param('in2', INPUT, TYPE_IMAGE),
                 param('convert_policy', INPUT, TYPE_ENUM, CONVERT_POLICY_WRAP),
                 param('out', OUTPUT, TYPE_IMAGE),
                 )

    @property
    def body(self):
        return "out = in1 %s in2;" % self.op


class MultiplyNode(ElementwiseNode):
    signature = (param('in1', INPUT, TYPE_IMAGE),
                 param('in2', INPUT, TYPE_IMAGE),
                 param('scale', INPUT, TYPE_SCALAR, 1),
                 param('convert_policy', INPUT, TYPE_ENUM, CONVERT_POLICY_WRAP),
                 param('round_policy', INPUT, TYPE_ENUM, ROUND_POLICY_TO_ZERO),
                 param('out', OUTPUT, TYPE_IMAGE))
    kernel_enum =  KERNEL_MULTIPLY

    @property
    def body(self):
        if self.round_policy == ROUND_POLICY_TO_ZERO:
            return "out = in1 * in2 * %r;" % self.scale
        elif self.round_policy == ROUND_POLICY_TO_NEAREST_EVEN:
            return "out = rint(in1 * in2 * %r);" % self.scale
        else:
            raise NotImplementedError


class DivideNode(ElementwiseNode):
    signature = (param('in1', INPUT, TYPE_IMAGE),
                 param('in2', INPUT, TYPE_IMAGE),
                 param('scale', INPUT, TYPE_SCALAR, 1),
                 param('convert_policy', INPUT, TYPE_ENUM, CONVERT_POLICY_WRAP),
                 param('round_policy', INPUT, TYPE_ENUM, ROUND_POLICY_TO_ZERO),
                 param('out', OUTPUT, TYPE_IMAGE))

    @property
    def body(self):
        if self.round_policy == ROUND_POLICY_TO_ZERO:
            return "out = (in1 * %r) / in2;" % self.scale
        elif self.round_policy == ROUND_POLICY_TO_NEAREST_EVEN:
            return "out = rint((in1 * %r) / in2);" % self.scale
        else:
            raise NotImplementedError


class TrueDivideNode(ElementwiseNode):
    signature = (param('in1', INPUT, TYPE_IMAGE),
                 param('in2', INPUT, TYPE_IMAGE),
                 param('out', OUTPUT, TYPE_IMAGE))

    body = "out = ((double) in1) / ((double) in2);"

    def verify(self):
        self.out.suggest_color(DF_IMAGE_F64)
        ElementwiseNode.verify(self)


class PowerNode(ElementwiseNode):
    signature = (param('in1', INPUT, TYPE_IMAGE),
                 param('in2', INPUT, TYPE_IMAGE),
                 param('convert_policy', INPUT, TYPE_ENUM, CONVERT_POLICY_WRAP),
                 param('out', OUTPUT, TYPE_IMAGE))

    body = "out = pow(in1, in2);"


class CompareNode(BinaryOperationNode):
    signature = (param('in1', INPUT, TYPE_IMAGE),
                 param('op', INPUT, TYPE_STRING),
                 param('in2', INPUT, TYPE_IMAGE),
                 param('out', OUTPUT, TYPE_IMAGE))
    convert_policy = CONVERT_POLICY_WRAP

    def verify(self):
        self.out.suggest_color(DF_IMAGE_U8)
        ElementwiseNode.verify(self)


class ColorConvertNode(Node):
    signature = (param('input', INPUT, TYPE_IMAGE),
                 param('output', OUTPUT, TYPE_IMAGE))
    convert_policy = CONVERT_POLICY_SATURATE
    kernel_enum = KERNEL_COLOR_CONVERT 

    def verify(self):
        for im in (self.input, self.output):
            if im.color_space != COLOR_SPACE_DEFAULT:
                raise NotImplementedError
            if im.channel_range != CHANNEL_RANGE_FULL:
                raise NotImplementedError
        self.output.ensure_shape(self.input)

    def compile(self, code):
        in_channels = self.input.image_format.channels
        out_channels = self.output.image_format.channels
        if CHANNEL_R in in_channels and CHANNEL_Y in out_channels:
            code.add_block(self, """
                double kr = 0.2126, kb = 0.0722, kg = 1.0 - kr - kb;
                for (long i = 0; i < out.pixels; i++) {
                    double r = ((double)input.channel_r[i]) / 256.0;
                    double g = ((double)input.channel_g[i]) / 256.0;
                    double b = ((double)input.channel_b[i]) / 256.0;
                    double y = kr*r + kg*g + kb*b;
                    double u = b/2.0 - (kr*r + kg*g) / (2.0 - 2.0*kb);
                    double v = r/2.0 - (kb*b + kg*g) / (2.0 - 2.0*kr);
                    out.channel_y[i] = y * 256.0;
                    out.channel_u[i] = floor(u * 256.0 + 128.0);
                    out.channel_v[i] = floor(u * 256.0 + 128.0);
                }
            """, out=self.output, input=self.input)
        else:
            raise NotImplementedError


class ChannelExtractNode(Node):
    signature = (param('input', INPUT, TYPE_IMAGE),
                 param('channel', INPUT, TYPE_ENUM),
                 param('output', OUTPUT, TYPE_IMAGE))
    kernel_enum = KERNEL_CHANNEL_EXTRACT 

    def verify(self):
        if self.channel not in self.input.image_format.channels:
            raise InvalidFormatError(
                'Cant extract CHANNEL_%s from %s image.' % (
                    channel_char[self.channel].upper(), self.input.image_format.__name__))
        self.output.ensure_color(DF_IMAGE_U8)
        self.output.ensure_shape(self.input)

    def compile(self, code):
        code.add_block(self, """
            for (long i = 0; i < out.pixels; i++) {
                out[i] = input.channel_%s[i];
            }
            """ % channel_char[self.channel],
                       input=self.input, out=self.output)


class ChannelCombineNode(Node):
    signature = (param('plane0', INPUT, TYPE_IMAGE),
                 param('plane1', INPUT, TYPE_IMAGE),
                 param('plane2', INPUT, TYPE_IMAGE, Unassigned),
                 param('plane3', INPUT, TYPE_IMAGE, Unassigned),
                 param('output', OUTPUT, TYPE_IMAGE))
    kernel_enum = KERNEL_CHANNEL_COMBINE

    def verify(self):
        self.output.suggest_color(DF_IMAGE_RGB)
        if self.output.color != DF_IMAGE_RGB:
            raise NotImplementedError
        if self.plane2 is Unassigned or self.plane3 is not Unassigned:
            raise InvalidNodeError('RGB image requires 3 planes.')
        self.output.ensure_shape(self.plane0)
        self.plane1.ensure_shape(self.plane0)
        self.plane2.ensure_shape(self.plane0)

    def compile(self, code):
        code.add_block(self, """
            for (long i = 0; i < out.pixels; i++) {
                out.channel_r[i] = red[i];
                out.channel_g[i] = green[i];
                out.channel_b[i] = blue[i];
            }
            """, red=self.plane0, green=self.plane1, blue=self.plane2, out=self.output)



class Gaussian3x3Node(Node):
    signature = (param('input', INPUT, TYPE_IMAGE),
                 param('output', OUTPUT, TYPE_IMAGE))
    kernel_enum = KERNEL_GAUSSIAN_3x3

    def verify(self):
        self.ensure(self.input.image_format.items == 1)
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


class Sobel3x3Node(Node):
    signature = (param('input', INPUT, TYPE_IMAGE),
                 param('output_x', OUTPUT, TYPE_IMAGE),
                 param('output_y', OUTPUT, TYPE_IMAGE))
    kernel_enum = KERNEL_SOBEL_3x3

    def verify(self):
        self.ensure(self.input.image_format.items == 1)
        if self.input.color in [DF_IMAGE_U8, DF_IMAGE_U16]:
            ot = DF_IMAGE_S16
        else:
            ot = signed_color(self.input.color)
        self.output_x.suggest_color(ot)
        self.output_y.suggest_color(ot)
        self.output_x.ensure_similar(self.input)
        self.output_y.ensure_similar(self.input)

    def compile(self, code):
        code.add_block(self, """
            for (long y = 0; y < img.height; y++) {
                for (long x = 0; x < img.width; x++) {
                    dx[x, y] = (-1*img[x-1, y-1] + 1*img[x+1, y-1] +
                                -2*img[x-1, y]   + 2*img[x+1, y]   +
                                -1*img[x-1, y+1] + 1*img[x+1, y+1]);
                    dy[x, y] = (-1*img[x-1, y-1] - 2*img[x, y-1] - 1*img[x+1, y-1] +
                                 1*img[x-1, y+1] + 2*img[x, y+1] + 1*img[x+1, y+1]);
                }
            }
            """, img=self.input, dx=self.output_x, dy=self.output_y)


class MagnitudeNode(ElementwiseNode):
    signature = (param('grad_x', INPUT, TYPE_IMAGE),
                 param('grad_y', INPUT, TYPE_IMAGE),
                 param('mag', OUTPUT, TYPE_IMAGE))
    convert_policy = CONVERT_POLICY_SATURATE
    kernel_enum = KERNEL_MAGNITUDE

    def verify(self):
        self.ensure(self.grad_x.image_format.items == 1)
        self.ensure(self.grad_y.image_format.items == 1)
        it = result_color(self.grad_x.image_format, self.grad_y.image_format)
        if it in [DF_IMAGE_U8, DF_IMAGE_U16, DF_IMAGE_S8, DF_IMAGE_S16]:
            ot = DF_IMAGE_U16
        else:
            ot = it
        self.mag.suggest_color(ot)
        self.mag.ensure_similar(self.grad_x)
        self.mag.ensure_similar(self.grad_y)

    body = "mag = sqrt( grad_x * grad_x + grad_y * grad_y );"


class PhaseNode(ElementwiseNode):
    signature = (param('grad_x', INPUT, TYPE_IMAGE),
                 param('grad_y', INPUT, TYPE_IMAGE),
                 param('orientation', OUTPUT, TYPE_IMAGE))
    kernel_enum = KERNEL_PHASE

    def verify(self):
        self.ensure(self.grad_x.image_format.items == 1)
        self.ensure(self.grad_y.image_format.items == 1)
        self.orientation.suggest_color(DF_IMAGE_U8)
        self.orientation.ensure_similar(self.grad_x)
        self.orientation.ensure_similar(self.grad_y)

    body = "orientation = (atan2(grad_y, grad_x) + M_PI) * (255.0 / 2.0 / M_PI);"


class AccumulateImageNode(Node):
    signature = (param('input', INPUT, TYPE_IMAGE),
                 param('accum', BIDIRECTIONAL, TYPE_IMAGE))
    kernel_enum = KERNEL_ACCUMULATE

    def verify(self):
        pass

mydir = os.path.dirname(os.path.abspath(__file__))


class PlayNode(Node):
    signature = (param('path', INPUT, TYPE_STRING),
                 param('output', OUTPUT, TYPE_IMAGE))
    player = None

    ffi = cffi.FFI()
    ffi.cdef("""
            struct avplay {
                int width, height;
                ...;
            };
            struct avplay *avplay_new(char *fn);
            int avplay_next(struct avplay *p, uint8_t *img);
            """)
    try:
        lib = ffi.verify(open(os.path.join(mydir, 'avplay.c')).read(),
                         extra_compile_args=['-O3'],
                         libraries=['avformat', 'avcodec', 'avutil',
                                    'swscale', 'avdevice'],
                         )
    except (cffi.VerificationError, IOError) as e:
        print e
        lib = None

    def verify(self):
        if self.lib is None:
            raise InvalidValueError('''
                                     
    PlayNode failed to compile. See error message from the compiler above,
    and make sure you have the libav libs installed. On Debian:

        apt-get install libavformat-dev libswscale-dev libavdevice-dev

    If pyvx is installed centraly it needs to be reinstalled after the
    issue have been resolved. Using pip that is achieved with:

        pip install --upgrade --force-reinstall pyvx

                ''')
        if not self.player:
            self.player = self.lib.avplay_new(str(self.path))
        if not self.player:
            raise InvalidValueError(
                "Unable to decode '%s'." % self.path)
        self.output.ensure_shape(self.player.width, self.player.height)
        self.output.ensure_color(DF_IMAGE_RGB)
        self.output.force()

    def compile(self, code):
        adr = int(self.ffi.cast('long', self.player))
        code.add_block(
            self, "if (avplay_next((void *)0x%x, img.data)) return VX_ERROR_GRAPH_ABANDONED;" % adr, img=self.output)
        code.extra_link_args.append(self.ffi.verifier.modulefilename)
        code.includes.add('#include "avplay.h"')

    # FIXME
    #def __del__(self):
    #    self.lib.avplay_release(self.player) 


class ShowNode(Node):
    signature = (param('input', INPUT, TYPE_IMAGE),
                 param('name', INPUT, TYPE_STRING, 'View'))
    viewer = None

    ffi = cffi.FFI()
    ffi.cdef("""
            struct glview {
                int width, height;
                ...;
            };
            struct glview *glview_create(int width, int height, int pixel_type, int pixel_size, char *name);
            int glview_next(struct glview *m, unsigned char *imageData);
            void glview_release(struct glview *m);

            #define GL_RGB ...
            #define GL_UNSIGNED_BYTE ...

             """)
    try:
        lib = ffi.verify(open(os.path.join(mydir, 'glview.c')).read(),
                         extra_compile_args=['-O3'],
                         libraries=['glut', 'GL', 'GLU'])
    except (cffi.VerificationError, IOError) as e:
        print e
        lib = None

    def verify(self):
        if self.lib is None:
            raise InvalidValueError('''

                ShowNode failed to compile. See error message from the compiler above,
                and make sure you have glut GL and GLU installed. On Debian:

                    apt-get install freeglut3-dev

                If pyvx is installed centraly it needs to be reinstalled after the
                issue have been resolved. Using pip that is achieved with:

                    pip install --upgrade --force-reinstall pyvx

                ''')
        self.input.ensure_color(DF_IMAGE_RGB)
        if self.viewer:
            if self.viewer.width == self.input.width and self.viewer.height == self.input.height:
                return
            self.lib.glview_release(self.viewer)
        self.viewer = self.lib.glview_create(self.input.width,
                                             self.input.height,
                                             self.lib.GL_RGB,
                                             self.lib.GL_UNSIGNED_BYTE,
                                             str(self.name))

    def compile(self, code):
        adr = int(self.ffi.cast('long', self.viewer))
        code.add_block(
            self, "if (glview_next((void *)0x%x, img.data)) return VX_ERROR_GRAPH_ABANDONED;" % adr, img=self.input)
        code.extra_link_args.append(self.ffi.verifier.modulefilename)
        code.includes.add('#include "glview.h"')

    def __del__(self):
        if self.viewer:
            self.lib.glview_release(self.viewer)

class Median3x3Node(Node):
    kernel_enum = KERNEL_MEDIAN_3x3
    signature = (param('input', INPUT, TYPE_IMAGE),
                 param('output', OUTPUT, TYPE_IMAGE))

    def verify(self):
        # FIXME
        self.output.ensure_similar(self.input)

    def compile(self, code):
        pass # FIXME

class HarrisCornersNode(Node):
    kernel_enum = KERNEL_HARRIS_CORNERS
    signature = (param('input', INPUT, TYPE_IMAGE),
                 param('strength_thresh', INPUT, TYPE_SCALAR),
                 param('min_distance', INPUT, TYPE_SCALAR),
                 param('sensitivity', INPUT, TYPE_SCALAR),
                 param('gradient_size', INPUT, TYPE_INT32),
                 param('block_size', INPUT, TYPE_INT32),
                 param('corners', OUTPUT, TYPE_ARRAY),
                 param('num_corners', OUTPUT, TYPE_SCALAR, Unassigned))

    def verify(self):
        pass # FIXME

    def compile(self, code):
        pass # FIXME
