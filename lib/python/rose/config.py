# -*- coding: utf-8 -*-
#-----------------------------------------------------------------------------
# (C) British Crown Copyright 2012-5 Met Office.
#
# This file is part of Rose, a framework for meteorological suites.
#
# Rose is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Rose is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Rose. If not, see <http://www.gnu.org/licenses/>.
#-----------------------------------------------------------------------------
"""Simple INI-style configuration data model, loader and dumper.

Synopsis:

    import rose.config
    try:
        config = rose.config.load(file)
    except rose.config.ConfigSyntaxError:
        # ... do something to handle exception
    value = config.get([section, option])
    # ...

    config.set([section, option], value)
    # ...
    rose.config.dump(config, file)

Classes:
    ConfigNode - represents an individual node (setting or section).
    ConfigDumper - dumps a configuration to stdout or a file.
    ConfigLoader - loads a configuration into a Config instance.

Functions:
    dump - shorthand for ConfigDumper().dump
    load - shorthand for ConfigLoader().load

Limitations:
 * The loader does not handle trailing comments.

What about the standard library ConfigParser? Well, it is problematic:
 * The comment character and style is hard-coded.
 * The assignment character is hard-coded.
 * A duplicated section header causes an exception to be raised.
 * Option keys are transformed to lower case by default.
 * It is far too complicated and confusing.

"""

import errno
import os.path
import re
from rose.env import env_var_escape
import shlex
import sys
from tempfile import NamedTemporaryFile


CHAR_ASSIGN = "="
CHAR_COMMENT = "#"
CHAR_SECTION_OPEN = "["
CHAR_SECTION_CLOSE = "]"

OPT_CONFIG_DIR = "opt"
REC_SETTING_ELEMENT = re.compile(r"^(.+?)\(([^)]+)\)$")

STATE_SECT_IGNORED = "^"


class ConfigNode(object):

    """Represent a node in a configuration file."""

    STATE_NORMAL = ""
    STATE_USER_IGNORED = "!"
    STATE_SYST_IGNORED = "!!"

    def __init__(self, value=None, state=STATE_NORMAL, comments=None):
        if value is None:
            value = {}
        if comments is None:
            comments = []
        self.value = value
        self.state = state
        self.comments = comments

    def __repr__(self):
        return str({"value": self.value,
                    "state": self.state,
                    "comments": self.comments})

    __str__ = __repr__

    def __len__(self):
        return len(self.value)

    def __getitem__(self, key):
        return self.value[key]

    def __setitem__(self, key, value):
        self.value[key] = value

    def __delitem__(self, key):
        return self.value.pop(key)

    def __iter__(self, key):
        return self.value.items()

    def __eq__(self, other):
        if self is other:
            return True
        try:
            for keys_1, node_1 in self.walk(no_ignore=True):
                node_2 = other.get(keys_1, no_ignore=True)
                if (type(node_1) != type(node_2) or
                        (not isinstance(node_1.value, dict) and
                         node_1.value != node_2.value) or
                        node_1.comments != node_2.comments):
                    return False
            for keys_2, node_2 in other.walk(no_ignore=True):
                if self.get(keys_2, no_ignore=True) is None:
                    return False
        except AttributeError:  # Should handle "other is None"
            return False
        return True

    def __ne__(self, other):
        return not self.__eq__(other)

    def is_ignored(self):
        """Return true if current node is in the "ignored" state."""
        return self.state != self.STATE_NORMAL

    def walk(self, keys=None, no_ignore=False):
        """Return all keylist - sub-node pairs below keys.

        keys is a list defining a hierarchy of node.value
        'keys'. If an entry in keys is the null string,
        it is skipped.

        If a sub-node is at the top level, and does not
        contain any node children, a null string will be
        prepended to the returned keylist.

        """
        if keys is None:
            keys = []
        start_node = self.get(keys, no_ignore)
        stack = [(keys, start_node)]
        if start_node is None:
            stack = []
        while stack:
            node_keys, node = stack.pop(0)
            if isinstance(node.value, dict):
                for key in node.value.keys():
                    child_keys = node_keys + [key]
                    subnode = self.get(child_keys, no_ignore)
                    if subnode is not None:
                        stack.insert(0, (child_keys, subnode))
            if node_keys == keys:
                continue
            if len(node_keys) == 1 and not isinstance(node.value, dict):
                null_node_keys = [""] + node_keys
                yield (null_node_keys, node)
            else:
                yield (node_keys, node)

    def get(self, keys=None, no_ignore=False):
        """Return a node at the position of keys, if any.

        keys is a list defining a hierarchy of node.value
        'keys'. If an entry in keys is the null string,
        it is skipped.

        no_ignore switches on filtering nodes by their
        ignored status. If True, ignored nodes will not be
        returned. If False, ignored nodes will be returned.

        """
        if not keys:
            return self
        keys = list(keys)
        node = self.get_filter(no_ignore)
        while node is not None and keys and keys[0] is not None:
            key = keys.pop(0)
            if not key:
                continue
            if isinstance(node.value, dict) and key in node.value:
                node = node.value[key].get_filter(no_ignore)
            else:
                node = None
        return node

    def get_filter(self, no_ignore):
        """Return None if no_ignore and node is in ignored state.

        Return self otherwise.

        """
        if no_ignore and self.state:
            return None
        return self

    def get_value(self, keys=None, default=None):
        """Return the value of a normal node at the position of keys, if any.

        If the node does not exist or is ignored, return None.

        """
        return getattr(self.get(keys, no_ignore=True), "value", default)

    def set(self, keys=None, value=None, state=None, comments=None):
        """Set node properties at the position of keys, if any.

        keys is a list defining a hierarchy of node.value
        'keys'. If an entry in keys is the null string,
        it is skipped.

        value defines the node.value property at this position.

        state defines the node.state property at this position.

        comments defines the node.comments property at this position.

        If state is None, the node.state property is unchanged.

        If comments is None, the node.comments property is unchanged.

        """
        if keys is None:
            keys = []
        else:
            keys = list(keys)
        if value is None:
            value = {}
        if not keys:
            return self
        node = self
        while keys and keys[0] is not None:
            key = keys.pop(0)
            if not key:
                continue
            if not isinstance(node.value, dict):
                node.value = {}
            if key not in node.value:
                node.value[key] = ConfigNode()
            node = node.value[key]
        node.value = value
        if state is not None:
            node.state = state
        if comments is not None:
            node.comments = comments
        return self

    def unset(self, keys=None):
        """Remove a node at this (keys) position, if any.

        keys is a list defining a hierarchy of node.value
        'keys'. If an entry in keys is the null string,
        it is skipped.

        """
        if keys is None:
            return None
        keys = list(keys)
        key = keys.pop()
        while keys and key is None:
            key = keys.pop()
        try:
            return self.get(keys).value.pop(key)
        except (KeyError, AttributeError):
            return None


class ConfigDumper(object):

    """Dumper of a ConfigNode object in Rose INI format."""

    def __init__(self, char_assign=CHAR_ASSIGN):
        """Initialise the configuration dumper utility.

        Arguments:
        char_assign -- the character to use to delimit a key=value assignment.

        """
        self.char_assign = char_assign

    def dump(self, root, target=sys.stdout, sort_sections=None,
             sort_option_items=None, env_escape_ok=False, concat_mode=False):
        """Format a ConfigNode object and write result to target.

        Arguments:
        root -- the root node, a ConfigNode object.
        target -- an open file handle or a string containing a
        file path. If not specified, the result is written to
        sys.stdout.
        sort_sections -- an optional argument that should be a function
        for sorting a list of section keys.
        sort_option_items -- an optional argument that should be a
        function for sorting a list of option (key, value) tuples.
        in string values.
        env_escape_ok -- an optional argument to indicate that $NAME
        and ${NAME} syntax in values should be escaped.
        concat_mode -- switch on concatenation mode. If True, add []
        before root level options.

        """
        if sort_sections is None:
            sort_sections = sort_settings
        if sort_option_items is None:
            sort_option_items = sort_settings
        handle = target
        if not hasattr(target, "write") or not hasattr(target, "close"):
            target_dir = os.path.dirname(target)
            if not target_dir:
                target_dir = "."
            if not os.path.isdir(target_dir):
                os.makedirs(target_dir)
            handle = NamedTemporaryFile(prefix=os.path.basename(target),
                                        dir=target_dir, delete=False)
        blank = ""
        if root.comments:
            for comment in root.comments:
                handle.write(self._comment_format(comment))
            blank = "\n"
        root_keys = root.value.keys()
        root_keys.sort(sort_sections)
        root_option_keys = []
        section_keys = []
        for key in root_keys:
            if isinstance(root.value[key].value, str):
                root_option_keys.append(key)
            else:
                section_keys.append(key)
        if root_option_keys:
            handle.write(blank)
            blank = "\n"
            if concat_mode:
                handle.write(CHAR_SECTION_OPEN + CHAR_SECTION_CLOSE + "\n")
            for key in root_option_keys:
                self._string_node_dump(
                    key, root.value[key], handle, env_escape_ok)
        for section_key in section_keys:
            section_node = root.value[section_key]
            handle.write(blank)
            blank = "\n"
            for comment in section_node.comments:
                handle.write(self._comment_format(comment))
            handle.write("%(open)s%(state)s%(key)s%(close)s\n" % {
                "open": CHAR_SECTION_OPEN,
                "state": section_node.state,
                "key": section_key,
                "close": CHAR_SECTION_CLOSE})
            keys = section_node.value.keys()
            keys.sort(sort_option_items)
            for key in keys:
                value = section_node.value[key]
                self._string_node_dump(key, value, handle, env_escape_ok)
        if handle is not target:
            handle.close()
            if not os.path.exists(target):
                open(target, "a").close()
            os.chmod(handle.name, os.stat(target).st_mode)
            os.rename(handle.name, target)

    __call__ = dump

    def _string_node_dump(self, key, node, handle, env_escape_ok):
        """Helper for self.dump().

        Return text representation of a string node.

        """
        state = node.state
        values = node.value.split("\n")
        for comment in node.comments:
            handle.write(self._comment_format(comment))
        value0 = values.pop(0)
        if env_escape_ok:
            value0 = env_var_escape(value0)
        handle.write(state + key + self.char_assign + value0)
        handle.write("\n")
        if values:
            indent = " " * len(state + key)
            for value in values:
                if env_escape_ok:
                    value = env_var_escape(value)
                handle.write(indent + self.char_assign + value + "\n")

    @classmethod
    def _comment_format(cls, comment):
        """Return text representation of a configuration comment."""
        return "#%s\n" % (comment)


class ConfigLoader(object):

    """Loader of an INI format configuration into a Config object."""

    RE_SECTION = re.compile(
        r"^(?P<head>\s*\[(?P<state>!?!?))(?P<section>.*)\]\s*$")
    TYPE_SECTION = "TYPE_SECTION"
    TYPE_OPTION = "TYPE_OPTION"
    UNKNOWN_NAME = "<???>"

    def __init__(self, char_assign=CHAR_ASSIGN, char_comment=CHAR_COMMENT):
        """Initialise the configuration utility.

        Arguments:
        char_comment -- the character to indicate the start of a
        comment.
        char_assign -- the character to use to delimit a key=value
        assignment.

        """
        self.char_assign = char_assign
        self.char_comment = char_comment
        self.re_option = re.compile(
            r"^(?P<state>!?!?)(?P<option>[^\s"
            + char_assign
            + r"]+)\s*"
            + char_assign
            + r"\s*(?P<value>.*)$")

    def load_with_opts(self, source, node=None, more_keys=None,
                       used_keys=None):
        """Read a source configuration file with optional configurations.

        Arguments:
        source -- A string for a file path.
        node --- A ConfigNode object if specified, otherwise created.
        more_keys -- A list of additional optional configuration names. If
                     source is "rose-${TYPE}.conf", the file of each name
                     should be "opt/rose-${TYPE}-${NAME}.conf".
        used_keys -- If defined, it should be a list for this method to append
                     to. The key of each successfully loaded optional
                     configuration will be appended to the list (unless the key
                     is already in the list). Missing optional configurations
                     that are specified in more_keys will not raise an error.
                     If not defined, any missing optional configuration will
                     trigger an OSError.

        Return node.

        """
        node = self.load(source, node)
        opt_conf_keys_node = node.unset(["opts"])
        if opt_conf_keys_node is None or opt_conf_keys_node.is_ignored():
            opt_conf_keys = []
        else:
            opt_conf_keys = shlex.split(opt_conf_keys_node.value)
        if more_keys:
            opt_conf_keys += more_keys
        if not opt_conf_keys:
            return node
        source_dir = os.path.dirname(source)
        source_root, source_ext = os.path.splitext(os.path.basename(source))
        for opt_conf_key in opt_conf_keys:
            key = opt_conf_key
            is_optional = (key.startswith("(") and key.endswith(")"))
            if is_optional:
                key = opt_conf_key[1:-1]
            opt_conf_file_name_base = source_root + "-" + key + source_ext
            opt_conf_file_name = os.path.join(
                source_dir, OPT_CONFIG_DIR, opt_conf_file_name_base)
            try:
                self.load(opt_conf_file_name, node)
            except IOError:
                if is_optional or (
                        used_keys is not None and opt_conf_key in more_keys):
                    continue
                raise
            else:
                if used_keys is not None and key not in used_keys:
                    used_keys.append(key)
        return node

    def load(self, source, node=None):
        """Read a source configuration file.

        Arguments:
        source -- an open file handle or a string for a file path.
        node --- a ConfigNode object if specified, otherwise created.

        Return node.

        """
        if node is None:
            node = ConfigNode()
        handle, file_name = self._get_file_and_name(source)
        keys = []
        type_ = None
        comments = None
        line_num = 0
        # Note: "for line in handle:" hangs for sys.stdin
        while True:
            line = handle.readline()
            if not line:
                break
            line_num += 1
            # White space and comments
            if line.isspace():
                comments = []
                continue
            elif line.lstrip().startswith(self.char_comment):
                if comments is None:
                    node.comments.append(self._comment_strip(line))
                else:
                    comments.append(self._comment_strip(line))
                continue
            # Handle option continuation.
            if type_ == self.TYPE_OPTION and line[0].isspace():
                value = node.get(keys[:]).value
                value_cont = line.strip()
                if value_cont.startswith(self.char_assign):
                    value_cont = value_cont[1:]
                node.set(keys[:], value + "\n" + value_cont)
                continue
            # Match a section header?
            match = self.RE_SECTION.match(line)
            if match:
                head, section, state = match.group("head", "section", "state")
                for char in [CHAR_SECTION_OPEN, CHAR_SECTION_CLOSE]:
                    index = section.find(char)
                    if index >= 0:
                        raise ConfigSyntaxError(
                            ConfigSyntaxError.BAD_CHAR,
                            file_name, line_num, len(head) + index, line)
                if type_ == self.TYPE_OPTION:
                    keys.pop()
                if keys:
                    keys.pop()
                section = section.strip()
                if section:
                    keys.append(section)
                    type_ = self.TYPE_SECTION
                else:
                    keys = []
                    type_ = None
                section_node = node.get(keys[:])
                if section_node is None:
                    node.set(keys[:], {}, state, comments)
                else:
                    section_node.state = state
                    if comments:
                        section_node.comments += comments
                comments = []
                continue
            # Match the start of an option setting
            match = self.re_option.match(line)
            if not match:
                raise ConfigSyntaxError(
                    ConfigSyntaxError.BAD_SYNTAX, file_name, line_num, 0, line)
            option, value, state = match.group("option", "value", "state")
            if type_ == self.TYPE_OPTION:
                keys.pop()
            keys.append(option)
            type_ = self.TYPE_OPTION
            value = value.strip()
            node.set(keys[:], value.strip(), state, comments)
            comments = []
        return node

    __call__ = load

    @classmethod
    def _comment_strip(cls, line):
        """Strip comment character and whitespace from a comment."""
        return line.strip()[1:]

    def _get_file_and_name(self, file_):
        """Return file handle and file name of "file_"."""
        if hasattr(file_, "readline"):
            try:
                file_name = file_.name
            except AttributeError:
                file_name = self.UNKNOWN_NAME
        else:
            file_name = os.path.abspath(file_)
            file_ = open(file_name, "r")
        return (file_, file_name)


class ConfigSyntaxError(Exception):

    """Exception raised for syntax error loading a configuration file.

    It has the following attributes:
        exc.code -- Error code. Can be one of:
            ConfigSyntaxError.BAD_CHAR (bad characters in a name)
            ConfigSyntaxError.BAD_SYNTAX (general syntax error)
        exc.file_name -- The name of the file that triggers the error.
        exc.line_num -- The line number (from 1) in the file with the error.
        exc.col_num -- The column number (from 0) in the line with the error.
        exc.line -- The content of the line that contains the error.
    """

    BAD_CHAR = "BAD_CHAR"
    BAD_SYNTAX = "BAD_SYNTAX"

    MESSAGES = {
        BAD_CHAR: """Unexpected character in name""",
        BAD_SYNTAX: """Expecting "[SECTION]" or "KEY=VALUE\"""",
    }

    def __init__(self, code, file_name, line_num, col_num, line):
        Exception.__init__(self, code, file_name, line_num, col_num, line)
        self.code = code
        self.file_name = file_name
        self.line_num = line_num
        self.col_num = col_num
        self.line = line

    def __str__(self):
        return "%s(%d): %s\n%s%s^" % (
            self.file_name,
            self.line_num,
            self.MESSAGES[self.code],
            self.line,
            " " * self.col_num)


def dump(root, target=sys.stdout, sort_sections=None, sort_option_items=None,
         env_escape_ok=False):
    """See ConfigDumper.dump for detail."""
    return ConfigDumper()(root, target, sort_sections, sort_option_items,
                          env_escape_ok)


def load(source, root=None):
    """See ConfigLoader.load for detail."""
    return ConfigLoader()(source, root)


def sort_element(elem_1, elem_2):
    """Sort pieces of text, numerically if possible."""
    if elem_1.isdigit():
        if elem_2.isdigit():
            return cmp(int(elem_1), int(elem_2))
        return -1
    elif elem_2.isdigit():
        return 1
    return cmp(elem_1, elem_2)


def sort_settings(setting_1, setting_2):
    """Sort sections and options, by numeric element if possible."""
    if (not isinstance(setting_1, basestring) or
            not isinstance(setting_2, basestring)):
        return cmp(setting_1, setting_2)
    match_1 = REC_SETTING_ELEMENT.match(setting_1)
    match_2 = REC_SETTING_ELEMENT.match(setting_2)
    if match_1 and match_2:
        text_1, num_1 = match_1.groups()
        text_2, num_2 = match_2.groups()
        if text_1 == text_2:
            return sort_element(num_1, num_2)
    return cmp(setting_1, setting_2)
