"""
:mod:`pyvx.codegen` --- Code generation tools
=============================================

"""

from pycparser import c_parser, c_ast
from pycparser.c_generator import CGenerator
from cffi import FFI
import tempfile
import subprocess
import os, re
from shutil import rmtree, copy

typedefs = ''.join("typedef int uint%d_t; typedef int int%d_t;" % (n, n)
                   for n in [8, 16, 32, 64])


def cparse(code):
    parser = c_parser.CParser()
    ast = parser.parse(typedefs + "void f() {" + code + "}")
    func = ast.ext[-1]
    # func.show()
    assert func.decl.name == 'f'
    return func.body


def cparse_signature(signature):
    parser = c_parser.CParser()
    ast = parser.parse(typedefs + signature + ';')
    ast = ast.ext[-1]
    return ast


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
        var, channel, index = self.get_magic_array_ref(node)
        if var is None:
            return CGenerator.visit_ArrayRef(self, node)
        return var.getitem(self.cxnode, channel, index)

    def get_magic_array_ref(self, node):
        var_name = None
        if isinstance(node.name, c_ast.StructRef):
            struct = node.name
            assert isinstance(struct.name, c_ast.ID)
            assert isinstance(struct.field, c_ast.ID)
            var_name = struct.name.name
            channel = struct.field.name
        elif isinstance(node.name, c_ast.ID):
            var_name = node.name.name
            channel = None

        if var_name in self.magic_vars:
            var = self.magic_vars[var_name]
            if isinstance(node.subscript, c_ast.ExprList):
                x, y = node.subscript.exprs
                index = (self.visit(x), self.visit(y))
            else:
                index = self.visit(node.subscript)
            return var, channel, index

        return None, None, None

    def visit_Assignment(self, node):
        var, channel, index = self.get_magic_array_ref(node.lvalue)
        if var is None:
            return CGenerator.visit_Assignment(self, node)
        return var.setitem(self.cxnode, channel, index,
                           node.op, self.visit(node.rvalue))


class Code(object):

    """ 
        Represents some generated C-code together with
        a bit of metadata. It has the following public attributes:

        ``indent_level``
            Number of spaces to indent code added using ``add_block``.
        ``extra_link_args``
            A ``list`` of extra arguments needed to be passed to the linker when 
            compiling the code. It is typically used to link with external libraries 
            used by the code.
        ``includes``
            A ``set`` of lines added at the top of the generated .c file outside
            the function enclosing the code. This is intended for ``#include ...``
            lines.

    """

    def __init__(self, code=''):
        """ Construct a new ``Code`` object and initiate it's code to ``code``.            
        """
        self.code = code
        self.indent_level = 0
        self.extra_link_args = []
        self.includes = set()

    def add_block(self, cxnode, code, **magic_vars):
        """ Append ``code`` as a new block of code. It will be enclosed in with ``{}``
            brackets to allow it to declare local variables. The code will be
            parsed and all references to the symbol names passed as keyword 
            arguments will be extracted and handled separately. These magic variables
            are intended to refere to ``Image`` objects, but could be anything that define 
            compatible ``getattr()`` and ``getitem()`` methods. If an ``Image`` is
            passed as the keyword argument ``img``, it can be used in the C-code in 
            the following ways:

                ``img[x,y]``
                    The value of pixel (``x``, ``y``) of a single channel image.
                ``img.channel_x[x,y]``
                    The value of pixel (``x``, ``y``) in ``CHANNEL_X``.
                ``img[i]``
                    The i'th value in the image. ``i`` is an integer between ``0``
                    and ``width * height * channels - 1``.
                ``img.channel_x[i]``
                    The i'th value in ``CAHNNEL_X`` of the image. ``i`` is an integer 
                    between ``0`` and ``width * height - 1``.           
                ``img.width``
                    The width of the image in pixels.
                ``img.height``
                    The height of the image in pixels.                
                ``img.pixels``
                    The number of pixels in the image (``width * height``).
                ``img.values``
                    The number of values in the image (``width * height * channels``).
                ``img.data``
                    A pointer to the beginning of the pixel data.


        """
        ast = cparse(code)
        # ast.show()
        generator = MagicCGenerator(cxnode, magic_vars)
        generator.indent_level = self.indent_level
        hdr = '\n%s// %s\n' % (' ' * self.indent_level,
                               cxnode.__class__.__name__)
        self.code += hdr + generator.visit(ast)

    def add_code(self, code):
        """ Extend the code with ``code`` without any adjustment.
        """
        self.code += code

    def __str__(self):
        """ Returns the generated code.
        """
        return self.code


def export(signature, add_ret_to_arg=None, retrive_args=True, store_result=True,
           exception_return=-1):
    def decorator(f):
        f.signature = signature
        f.add_ret_to_arg = add_ret_to_arg
        f.retrive_args = retrive_args
        f.store_result = store_result
        f.exception_return = exception_return
        return staticmethod(f)
    return decorator

class CApiBuilder(object):
    def __init__(self, ffi):
        self.ffi = ffi
        self.cdef = []
        self.stubs = []
        self.callbacks = {}
        self.wrapped_reference_types = set()
        self.exception_return_values = {}
        self.includes = set()

    def add_wrapped_reference_type(self, ctype):
        self.wrapped_reference_types.add(self.ffi.typeof(ctype))

    def add_function(self, cdecl, method):
        tp = self.ffi._typeof(cdecl, 
                              consider_function_as_funcptr=True)
        n = re.search(r'^\s*[^\s]+\s*\*?\s*([^\(\s]+)\s*\(', cdecl).group(1) # XXX: use parser
        callback_var = self.ffi.getctype(tp, '_' + n)
        callback_var = callback_var.replace('()', '(void)')
        self.cdef.append("%s;" % callback_var)
        args = ', '.join(self.ffi.getctype(t, 'a%d' % i)
                         for i, t in enumerate(tp.args))
        stub = self.ffi.getctype(tp.result, '%s(%s)' % (n, args))
        args = ', '.join('a%d' % i for i in xrange(len(tp.args)))
        if tp.result == 'void':
            stub += '{_%s(%s);}' % (n, args)
        else:
            stub += '{return _%s(%s);}' % (n, args)
        self.stubs.append(stub)
        self.callbacks[n] = self.make_callback(tp, method)

    def make_callback(self, tp, fn):
        store_result = tp.result in self.wrapped_reference_types
        retrive_refs = tuple([i for i, a in enumerate(tp.args)
                              if a in self.wrapped_reference_types])
        exception_return = self.exception_return_values.get(tp.result, 0)

        def f(*args):
            args = list(args)
            try:
                for i in retrive_refs:
                    args[i] = self.ffi.from_handle(self.ffi.cast('void *', args[i]))
                r = fn(*args)
                if store_result:
                    r = r.new_handle()
            except Exception as e:
                return self.exception_handler(e, tp.result)
            return r
        f.__name__ = fn.__name__
        return self.ffi.callback(tp, f)

    def build(self, name, version, soversion, out_path):
        setup = '\n'.join('"%s\\n"' % l for l in self.setup.split('\n'))
        tmp = tempfile.mkdtemp()
        try:
            src = os.path.join(tmp, "tmp.c")
            with open(src, 'w') as fd:
                fd.write("""
                    #include <stdint.h>
                    #include <Python.h>

                    static void __initialize(void) __attribute__((constructor));
                    void __initialize(void) {
                      Py_Initialize();
                      PyEval_InitThreads();                  
                      PyRun_SimpleString(%s);
                    }

                    static void __deinitialize(void) __attribute__((destructor));
                    void __deinitialize(void) {
                      Py_Finalize();
                    }
                    """ % setup +
                         '\n'.join(self.includes) + "\n\n" +
                         '\n'.join(self.cdef) + "\n\n" + 
                         '\n'.join(self.stubs))

            from distutils.core import Extension
            from cffi.ffiplatform import compile
            mydir = os.path.dirname(os.path.abspath(__file__))
            d = os.path.join(mydir, 'inc', 'headers')
            fn = compile(tmp, Extension(name='lib' + name,
                                        sources=[src],
                                        extra_compile_args=["-I" + d],
                                        extra_link_args=['-lpython2.7',
                                                         '-Wl,-soname,lib%s.so.' % name + soversion]))
            bfn = os.path.join(out_path, os.path.basename(fn))
            full = bfn + '.' + version
            so = bfn + '.' + soversion
            for f in [full, so, bfn]:
                try:
                    os.unlink(f)
                except OSError:
                    pass
            copy(fn, full)
            os.symlink(os.path.basename(full), so)
            os.symlink(os.path.basename(full), bfn)
        finally:
            rmtree(tmp)
        self.library_names = [full, so, bfn]

    def load(self):
        ffi = FFI()
        ffi.include(self.ffi)
        ffi.cdef('\n'.join(self.cdef))
        lib = ffi.dlopen(None)
        for n, cb in self.callbacks.items():
            setattr(lib, '_' + n, cb)
        self.lib = lib
