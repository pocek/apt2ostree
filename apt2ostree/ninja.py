import errno
import hashlib
import os
import re
import shlex
import sys
import textwrap
import traceback

from . import ninja_syntax

NINJA_AUTO_VARS = set(["in", "out", "_args_digest"])
ALREADY_WRITTEN = "ALREADY_WRITTEN"


class Ninja(ninja_syntax.Writer):
    builddir = "_build"

    def __init__(self, regenerate_command=None, width=78, debug=True,
                 ninjafile="build.ninja", standalone=True):
        if regenerate_command is None:
            regenerate_command = sys.argv

        self.debug = debug
        self.ninjafile = ninjafile
        self.standalone = standalone

        output = open(self.ninjafile + '~', 'w')
        super(Ninja, self).__init__(output, width)
        self.global_vars = {}
        self.targets = {}
        self.rules = {}
        self.generator_deps = set()

        self.add_generator_dep(__file__)
        self.add_generator_dep(ninja_syntax.__file__)
        self.add_generator_dep(__file__ + '/../ostree.py')
        self.add_generator_dep(__file__ + '/../multistrap.py')

        self.regenerate_command = regenerate_command
        self.variable("builddir", self.builddir)
        self.build(".FORCE", "phony")

        if self.standalone:
            # Write a reconfigure script to rememeber arguments passed to
            # configure:
            reconfigure = "%s/reconfigure-%s" % (self.builddir, ninjafile)
            self.add_target(reconfigure)
            try:
                os.mkdir(self.builddir)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
            with open(reconfigure, 'w') as f:
                f.write("#!/bin/sh\nexec %s\n" % (
                    shquote(["./" + os.path.relpath(self.regenerate_command[0])]
                            + self.regenerate_command[1:])))
            os.chmod(reconfigure, 0o755)
            self.rule("configure", reconfigure, generator=True)

        self.add_target("%s/.ninja_deps" % self.builddir)
        self.add_target("%s/.ninja_log" % self.builddir)

    def close(self):
        if not self.output.closed:
            if self.standalone:
                self.build(self.ninjafile, "configure",
                           list(self.generator_deps))
            super(Ninja, self).close()
            os.rename(self.ninjafile + '~', self.ninjafile)

    def __enter__(self):
        return self

    def __exit__(self, _1, _2, _3):
        self.close()

    def __del__(self):
        self.close()

    def variable(self, key, value, indent=0):
        if indent == 0:
            if key in self.global_vars:
                if value != self.global_vars[key]:
                    raise RuntimeError(
                        "Setting key to %s, when it was already set to %s" % (
                            key, value))
                return
            self.global_vars[key] = value
        super(Ninja, self).variable(key, value, indent)

    def build(self, outputs, rule, inputs=None,
              allow_non_identical_duplicates=False,
              **kwargs):  # pylint: disable=arguments-differ
        outputs = ninja_syntax.as_list(outputs)
        inputs = ninja_syntax.as_list(inputs)
        for x in outputs:
            s = hashlib.sha256()
            s.update(str((rule, inputs, sorted(kwargs.items()))).encode('utf-8'))
            try:
                if self.add_target(x, s.hexdigest()) == ALREADY_WRITTEN:
                    # Its a duplicate build statement, but it's identical to the
                    # last time it was written so that's ok.
                    return outputs
            except DuplicateTarget:
                if allow_non_identical_duplicates:
                    return outputs
                else:
                    raise
        if self.debug:
            self.output.write("# Generated by:\n")
            stack = traceback.format_stack()[:-1]
            for frame in stack:
                for line in frame.split("\n"):
                    if line:
                        self.output.write("# ")
                        self.output.write(line)
                        self.output.write("\n")
        return super(Ninja, self).build(outputs, rule, inputs=inputs, **kwargs)

    def rule(self, name, *args, **kwargs):  # pylint: disable=arguments-differ
        if name in self.rules:
            assert self.rules[name] == (args, kwargs)
        else:
            self.rules[name] = (args, kwargs)
            super(Ninja, self).rule(name, *args, **kwargs)

    def open(self, filename, mode='r', **kwargs):
        if 'w' in mode:
            self.add_target(filename)
        if 'r' in mode:
            try:
                out = open(filename, mode, **kwargs)
                self.add_generator_dep(filename)
                return out
            except IOError as e:
                if e.errno == errno.ENOENT:
                    # configure output depends on the existance of this file.
                    # It doesn't exist right now but we'll want to rerun
                    # configure if that changes.  The mtime of the containing
                    # directory will be updated when the file is created so we
                    # add a dependency on that instead:
                    self.add_generator_dep(os.path.dirname(filename) or '.')
                    raise
                else:
                    raise
        else:
            return open(filename, mode, **kwargs)

    def add_generator_dep(self, filename):
        """Cause configure to be rerun if changes are made to filename"""
        self.generator_deps.add(
            os.path.relpath(filename).replace('.pyc', '.py'))

    def add_target(self, target, rulehash=None):
        if not target:
            raise RuntimeError("Invalid target filename %r" % target)
        if target in self.targets:
            if self.targets[target] == rulehash:
                return ALREADY_WRITTEN
            else:
                raise DuplicateTarget(
                    "Duplicate target %r with different rule" % target)
        else:
            self.targets[target] = rulehash
            return None

    def write_gitignore(self, filename=None):
        if filename is None:
            filename = "%s/.gitignore" % self.builddir
        self.add_target(filename)
        with open(filename, 'w') as f:
            for x in self.targets:
                f.write("%s\n" % os.path.relpath(x, os.path.dirname(filename)))


class DuplicateTarget(RuntimeError):
    pass


def _is_string(val):
    if sys.version_info[0] >= 3:
        str_type = str
    else:
        str_type = basestring
    return isinstance(val, str_type)


def vars_in(items):
    if items is None:
        return set()
    if _is_string(items):
        items = [items]
    out = set()
    for text in items:
        for x in text.split('$$'):
            out.update(re.findall(r"\$(\w+)", x))
            out.update(re.findall(r"\${(\w+)}", x))
            for line in x.split('\n'):
                m = re.search(r'\$[^_{0-9a-zA-Z]', line)
                if m:
                    raise RuntimeError(
                        "bad $-escape (literal $ must be written as $$)\n"
                        "%s\n"
                        "%s^ near here" % (line, " " * m.start()))
    return out


class Rule(object):
    def __init__(self, name, command, outputs=None, inputs=None,
                 description=None, order_only=None, implicit=None,
                 output_type=None, allow_non_identical_duplicates=False,
                 **kwargs):
        if order_only is None:
            order_only = []
        if implicit is None:
            implicit = []
        self.name = name
        self.command = textwrap.dedent(command)
        self.outputs = ninja_syntax.as_list(outputs)
        self.inputs = ninja_syntax.as_list(inputs)
        self.order_only = ninja_syntax.as_list(order_only)
        self.implicit = ninja_syntax.as_list(implicit)
        self.output_type = output_type
        self.kwargs = kwargs
        self.allow_non_identical_duplicates = allow_non_identical_duplicates

        self.vars = vars_in(command).union(vars_in(inputs)).union(vars_in(outputs))

        if description is None:
            description = "%s(%s)" % (self.name, ", ".join(
                "%s=$%s" % (x, x) for x in self.vars))
        self.description = description

    def build(self, ninja, outputs=None, inputs=None, implicit=None,
              order_only=None, implicit_outputs=None, pool=None, **kwargs):
        if outputs is None:
            outputs = []
        if inputs is None:
            inputs = []
        if order_only is None:
            order_only = []
        if implicit is None:
            implicit = []
        ninja.newline()
        ninja.rule(self.name, self.command, description=self.description,
                   **self.kwargs)
        v = set(kwargs.keys())
        missing_args = self.vars - v - set(ninja.global_vars.keys()) - NINJA_AUTO_VARS
        if missing_args:
            raise TypeError("Missing arguments to rule %s: %s" %
                            (self.name, ", ".join(missing_args)))
        if v - self.vars:
            raise TypeError("Rule %s got unexpected arguments: %s" %
                            (self.name, ", ".join(v - self.vars)))

        if '_args_digest' in self.vars:
            s = hashlib.sha256()
            s.update(str([self.name] + sorted(kwargs.items())).encode('utf-8'))
            kwargs['_args_digest'] = s.hexdigest()[:7]

        if self.outputs:
            outputs.extend(ninja_syntax.expand(x, ninja.global_vars, kwargs)
                           for x in self.outputs)
        if self.inputs:
            inputs.extend(ninja_syntax.expand(x, ninja.global_vars, kwargs)
                          for x in self.inputs)
        if self.implicit:
            implicit.extend(ninja_syntax.expand(x, ninja.global_vars, kwargs)
                          for x in self.implicit)

        ninja.newline()
        outputs = ninja.build(
            outputs, self.name, inputs=inputs,
            implicit=implicit, order_only=self.order_only + order_only,
            implicit_outputs=implicit_outputs, pool=pool, variables=kwargs,
            allow_non_identical_duplicates=self.allow_non_identical_duplicates)
        if self.output_type:
            if isinstance(self.output_type, tuple):
                assert len(outputs) == len(self.output_type)
                outputs = tuple(t(x) for t, x in zip(self.output_type, outputs))
            else:
                assert len(outputs) == 1
                outputs = self.output_type(outputs[0])
        return outputs


def shquote(v):
    if _is_string(v):
        return shlex.quote(v)
    else:
        return " ".join(shquote(x) for x in v)
