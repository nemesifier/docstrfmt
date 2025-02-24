import itertools
import re
import string
from collections import namedtuple
from copy import copy
from math import floor
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

import black
import docutils
from attr import dataclass
from blib2to3.pgen2.tokenize import TokenError
from docutils.frontend import OptionParser
from docutils.nodes import pending
from docutils.parsers import rst
from docutils.utils import Reporter, new_document, unescape

from . import rst_extras
from .exceptions import InvalidRstError, InvalidRstErrors
from .util import get_code_line, make_enumerator

T = TypeVar("T")

section_chars = "=-~+.'\"`^_*:#"

# https://docutils.sourceforge.io/docs/ref/rst/restructuredtext.html#inline-markup-recognition-rules
space_chars = set(string.whitespace)
pre_markup_break_chars = space_chars | set("-:/'\"<([{")
post_markup_break_chars = space_chars | set("-.,:;!?\\/'\")]}>")

chain = itertools.chain.from_iterable


class FormatContext:
    def __init__(self, width, current_file, manager, black_config=None, **kwargs):
        self.width = width
        self.current_file = current_file
        self.manager = manager
        self.black_config = black_config
        self.starting_width = width
        self.bullet = ""
        self.column_widths = []
        self.current_ordinal = 0
        self.first_line_len = 0
        self.line_block_depth = 0
        self.ordinal_format = "arabic"
        self.section_depth = 0
        self.subsequent_indent = 0
        for key, value in kwargs.items():
            setattr(self, key, value)

    def _replace(self, **kwargs):
        current_context = copy(vars(self))
        for key, value in current_context.items():
            kwargs.setdefault(key, value)
        return self.__class__(**kwargs)

    def in_line_block(self):
        return self._replace(line_block_depth=self.line_block_depth + 1)

    def in_section(self):
        return self._replace(section_depth=self.section_depth + 1)

    def indent(self, spaces: int):
        return self._replace(width=max(1, self.width - spaces))

    def sub_indent(self, subsequent_indent: int):
        return self._replace(subsequent_indent=subsequent_indent)

    def with_bullet(self, bullet: str):
        return self._replace(bullet=bullet)

    def with_ordinal(self, current_ordinal: int):
        return self._replace(current_ordinal=current_ordinal)

    def with_ordinal_format(self, ordinal_format: str):
        return self._replace(ordinal_format=ordinal_format)

    def with_column_widths(self, widths: List[int]):
        return self._replace(column_widths=widths)

    def with_width(self, width: int):
        return self._replace(width=width)

    def wrap_first_at(self, width: int):
        return self._replace(first_line_len=width)


def pairwise(items: Iterable[T]) -> Iterator[Tuple[T, T]]:
    a, b = itertools.tee(items)
    next(b, None)
    return zip(a, b)


class inline_markup:
    def __init__(self, text: str) -> None:
        self.text = text


inline_item = Union[str, inline_markup]
inline_iterator = Iterator[inline_item]
line_iterator = Iterator[str]

word_info = namedtuple(
    "word_info",
    ["text", "in_markup", "start_space", "end_space", "start_punct", "end_punct"],
)


@dataclass
class CodeFormatters:
    code: str
    content_offset: int
    context: FormatContext

    def python(self) -> str:
        try:
            self.code = black.format_str(
                self.code, mode=self.context.black_config
            ).rstrip()
        except (UserWarning, black.InvalidInput, TokenError):
            try:
                compile(self.code, "<code-block>", mode="exec")
            except SyntaxError as syntax_error:
                self.context.manager.error_count += 1
                document_line = get_code_line(
                    self.context.current_file, self.code, True
                ) - len(self.code.splitlines())
                if self.context.manager.reporter:
                    self.context.manager.reporter.error(
                        f"SyntaxError: {syntax_error.msg}:\n\nFile"
                        f' "{self.context.current_file}", line'
                        f' {document_line + syntax_error.lineno}:\n{syntax_error.text}\n{" " * (syntax_error.offset - 1)}^'
                    )
        return self.code


class Formatters:
    def __init__(self, manager):
        self.manager = manager

    @staticmethod
    def _chain_with_line_separator(
        separator: T, items: Iterable[Iterable[T]]
    ) -> Iterator[T]:
        first = True
        for item in items:
            if not first:
                yield separator
            first = False
            yield from item

    @staticmethod
    def _divide_evenly(width, column_count):
        evenly = [floor(width / column_count)] * column_count
        for i in range(width % column_count):
            evenly[-1 - i] += 1
        return evenly

    @staticmethod
    def _enum_first(items: Iterable[T]) -> Iterator[Tuple[bool, T]]:
        return zip(itertools.chain([True], itertools.repeat(False)), items)

    def _format_children(
        self, node: docutils.nodes.Node, context
    ) -> Iterator[Iterator[str]]:
        return (
            self.manager.perform_format(child, context)
            if index == 0
            else self.manager.perform_format(child, context.wrap_first_at(0))
            for index, child in enumerate(node.children)
        )

    def _generate_table_matrix(self, context, rows, width, widths=None):
        if widths:
            return [
                [
                    max(
                        [
                            len(line)
                            for child in self._format_children(
                                cell, context.with_width(width=widths[column_index])
                            )
                            for line in child
                        ]
                        or [0]
                    )
                    for column_index, cell in enumerate(row)
                ]
                for row in rows
            ]
        return [
            [
                max(
                    [
                        len(line)
                        for child in self._format_children(
                            cell, context.with_width(width=width)
                        )
                        for line in child
                    ]
                    or [0]
                )
                for cell in row
            ]
            for row in rows
        ]

    def _list(self, node: docutils.nodes.Node, context) -> line_iterator:
        sub_children = []
        for child_index, child in enumerate(node.children, 1):
            sub_children.append(
                list(self.manager.perform_format(child, context))
                + (
                    [""]
                    if len(child.children) > 1 and len(node.children) != child_index
                    else []
                )
            )

        yield from chain(sub_children)

    def _prepend_if_any(self, prefix: T, items: Iterator[T]) -> Iterator[T]:
        try:
            item = next(items)
        except StopIteration:
            return
        yield prefix
        yield item
        yield from items

    def _sub_admonition(self, node: docutils.nodes.Node, context) -> line_iterator:
        yield f".. {node.tagname}::"
        yield ""
        yield from self._with_spaces(
            4,
            self._chain_with_line_separator(
                "", self._format_children(node, context.indent(4))
            ),
        )

    attention = _sub_admonition
    caution = _sub_admonition
    danger = _sub_admonition
    error = _sub_admonition
    hint = _sub_admonition
    important = _sub_admonition
    note = _sub_admonition
    seealso = _sub_admonition
    tip = _sub_admonition
    warning = _sub_admonition

    def _with_spaces(self, space_count: int, lines: Iterable[str]) -> Iterator[str]:
        spaces = " " * space_count
        for line in lines:
            yield spaces + line if line else line

    @staticmethod
    def _wrap_text(
        width: Optional[int], items: Iterable[inline_item], context: FormatContext
    ) -> Iterator[str]:
        if width is not None and width <= 0:
            raise ValueError(f"Invalid starting width {context.starting_width}")
        raw_words = []
        for item in list(items):
            new_words = []
            if isinstance(item, str):
                if not item:  # pragma: no cover
                    # An empty string is treated as having trailing punctuation: it only
                    # shows up when two inline markup blocks are separated by
                    # backslash-space, and this means that after it is merged with its
                    # predecessor the resulting word will not cause a second escape to
                    # be introduced when merging with the successor.
                    new_words = [word_info(item, False, False, False, False, True)]
                else:
                    new_words = [
                        word_info(word, False, False, False, False, False)
                        for word in item.split()
                    ]
                    if item:
                        if not new_words:
                            new_words = [word_info("", False, True, True, True, True)]
                        if item[0] in space_chars:
                            new_words[0] = new_words[0]._replace(start_space=True)
                        if item[-1] in space_chars:
                            new_words[-1] = new_words[-1]._replace(end_space=True)
                        if item[0] in post_markup_break_chars:
                            new_words[0] = new_words[0]._replace(start_punct=True)
                        if item[-1] in pre_markup_break_chars:
                            new_words[-1] = new_words[-1]._replace(end_punct=True)
            elif isinstance(item, inline_markup):
                new_words = [
                    word_info(word, True, False, False, False, False)
                    for word in item.text.split()
                ]
            raw_words.append(new_words)
        raw_words = list(chain(raw_words))
        words = [word_info("", False, True, True, True, True)]
        for word in raw_words:
            last = words[-1]
            if not last.in_markup and word.in_markup and not last.end_space:
                join = "" if last.end_punct else r"\ "
                words[-1] = word_info(
                    last.text + join + word.text, True, False, False, False, False
                )
            elif last.in_markup and not word.in_markup and not word.start_space:
                join = "" if word.start_punct else r"\ "
                words[-1] = word_info(
                    last.text + join + word.text,
                    False,
                    False,
                    word.end_space,
                    word.start_punct,
                    word.end_punct,
                )
            else:
                words.append(word)

        word_strings = (word.text for word in words if word.text)

        if width is None:
            yield " ".join(word_strings)
            return

        words: List[str] = []
        current_line_length = 0
        if context.first_line_len:
            width -= context.first_line_len
        for word in word_strings:
            next_line_len = (
                current_line_length
                + (context.subsequent_indent if bool(words) else 0)
                + bool(words)
                + len(word)
            )
            if words and next_line_len > width:
                yield " " * context.subsequent_indent + " ".join(words)
                if context.first_line_len:
                    width += context.first_line_len
                    context.first_line_len = None
                words = []
                next_line_len = len(word)
            words.append(word)
            current_line_length = next_line_len
        if words:
            yield " ".join(words)

    def admonition(self, node: docutils.nodes.admonition, context) -> line_iterator:
        title = node.children[0]
        assert isinstance(title, docutils.nodes.title)
        yield (
            ".. admonition::"
            f" {''.join(self._wrap_text(None, chain(self._format_children(title, context)), context))}"
        )
        yield ""
        context = context.indent(4)
        yield from self._with_spaces(
            4,
            self._chain_with_line_separator(
                "",
                (
                    self.manager.perform_format(child, context)
                    for child in node.children[1:]
                ),
            ),
        )

    def block_quote(self, node: docutils.nodes.block_quote, context) -> line_iterator:
        yield from self._with_spaces(
            4,
            self._chain_with_line_separator(
                "", self._format_children(node, context.indent(4))
            ),
        )

    def bullet_list(self, node: docutils.nodes.bullet_list, context) -> line_iterator:
        yield from self._list(node, context.with_bullet("-"))

    def comment(self, node: docutils.nodes.comment, context) -> line_iterator:
        yield ".."
        if node.children:
            text = "\n".join(chain(self._format_children(node, context)))
            yield from self._with_spaces(4, text.splitlines())

    def definition(self, node: docutils.nodes.definition, context) -> line_iterator:
        yield from self._chain_with_line_separator(
            "", self._format_children(node, context)
        )

    def definition_list(
        self, node: docutils.nodes.definition_list, context
    ) -> line_iterator:
        yield from self._chain_with_line_separator(
            "", self._format_children(node, context)
        )

    def definition_list_item(
        self, node: docutils.nodes.definition_list_item, context
    ) -> line_iterator:
        for child in node.children:
            if isinstance(child, docutils.nodes.term):
                yield from self.manager.perform_format(child, context)
            elif isinstance(child, docutils.nodes.definition):
                yield from self._with_spaces(
                    4, self.manager.perform_format(child, context.indent(4))
                )

    def directive(self, node: docutils.nodes.Node, context) -> line_iterator:
        directive = node.attributes["directive"]

        is_code_block = directive.name in ["code", "code-block"]

        yield " ".join(
            [
                f".. {'code-block' if is_code_block else directive.name}::",
                *directive.arguments,
            ]
        )
        # Just rely on the order being stable, hopefully.
        for k, v in directive.options.items():
            yield f"    :{k}:" if v is None else f"    :{k}: {v}"

        if is_code_block:
            language = directive.arguments[0] if directive.arguments else None
            text = "\n".join(directive.content.data)
            try:
                func = getattr(
                    CodeFormatters(text, directive.content_offset, context), language
                )
                text = func()
            except (AttributeError, TypeError):
                pass
            yield ""
            yield from self._with_spaces(4, text.splitlines())
        else:
            if directive.raw:
                yield from self._prepend_if_any(
                    "", self._with_spaces(4, directive.content)
                )
            else:
                sub_doc = self.manager.parse_string(
                    context.current_file, "\n".join(directive.content)
                )
                if sub_doc.children:
                    yield ""
                    yield from self._with_spaces(
                        4, self.manager.perform_format(sub_doc, context.indent(4))
                    )

    def document(self, node: docutils.nodes.document, context) -> line_iterator:
        yield from self._chain_with_line_separator(
            "", self._format_children(node, context)
        )

    def emphasis(self, node: docutils.nodes.emphasis, context) -> inline_iterator:
        joined = "".join(chain(self._format_children(node, context))).replace(
            "*", "\\*"
        )
        yield inline_markup(f"*{joined}*")

    def enumerated_list(
        self, node: docutils.nodes.enumerated_list, context
    ) -> line_iterator:
        yield from self._list(
            node,
            context.with_ordinal(node.attributes.get("start", 1)).with_ordinal_format(
                node.attributes["enumtype"]
            ),
        )
        context.current_ordinal = 0

    def field(self, node: docutils.nodes.field, context) -> line_iterator:
        children = chain(self._format_children(node, context))
        field_name = next(children)
        is_empty_sphinx_metadata_field = field_name.startswith(
            (":nocomments", ":nosearch", ":orphan")
        )
        try:
            first_line = next(children)
            if first_line and is_empty_sphinx_metadata_field:
                raise InvalidRstError(
                    context.current_file,
                    "ERROR",
                    get_code_line(
                        context.current_file, f":{node.astext().strip()}:", strict=True
                    ),
                    f"Non-empty Sphinx `:{node.astext().strip()}:` metadata"
                    " field. Please remove field body or omit completely.",
                )
        except StopIteration:
            if is_empty_sphinx_metadata_field:
                yield from chain(self._format_children(node, context))
                return
            raise InvalidRstError(
                context.current_file,
                "ERROR",
                get_code_line(
                    context.current_file, f":{node.astext().strip()}:", strict=True
                ),
                f"Empty `:{node.astext().strip()}:` field. Please add a field body or"
                " omit completely.",
            )

        children = list(children)
        children_processed = []
        for i, child in enumerate(children):
            if child.startswith(".."):
                blocks_in_child = [child]
                for block in children[i + 1 :]:
                    if block.startswith("    "):
                        blocks_in_child.append(block)
                    elif block == "":
                        blocks_in_child.append(block)
                    else:  # pragma: no cover
                        break
                del children[i : i + len(blocks_in_child) - 1]
                if blocks_in_child[-1] != "":
                    blocks_in_child.append("")
                children_processed += blocks_in_child
            else:
                children_processed.append(child)
        children = children_processed
        yield f"{field_name} {first_line}"
        yield from self._with_spaces(4, children)

    def field_body(self, node: docutils.nodes.field_body, context) -> line_iterator:
        yield from self._chain_with_line_separator(
            "",
            self._format_children(
                node,
                context.indent(4).wrap_first_at(
                    len(f":{node.parent.children[0].astext()}: ") - 4
                ),
            ),
        )

    def field_list(self, node: docutils.nodes.field_list, context) -> line_iterator:
        param_fields = []
        param_types = {}
        var_fields = []
        var_types = {}
        returns_fields = []
        rtype_fields = []
        raises_fields = []
        other_fields = []
        field_types_mapping = {
            "param": param_fields,
            "var": var_fields,
            "returns": returns_fields,
            "rtype": rtype_fields,
            "raises": raises_fields,
        }
        field_name_mapping = {
            "arg": "param",
            "argument": "param",
            "key": "param",
            "keyword": "param",
            "param": "param",
            "parameter": "param",
            "return": "returns",
            "returns": "returns",
            "except": "raises",
            "exception": "raises",
            "raise": "raises",
            "raises": "raises",
            "cvar": "var",
            "ivar": "var",
            "var": "var",
        }
        already_typed = []
        children = node.children
        for child in children[:]:
            field_body = child.children[0].children[0]
            try:
                field_kind, *field_typing, field_name = field_body.split(" ")
            except ValueError:
                field_kind = field_body.split(" ")[0]
                field_typing = []
                field_name = None
            new_field_kind = field_name_mapping.get(field_kind, field_kind)
            if field_kind != new_field_kind:
                to_join = [new_field_kind, *field_typing]
                if field_name:
                    to_join.append(field_name)
                child.children[0].replace_self(
                    docutils.nodes.field_name("", " ".join(to_join))
                )
                field_kind = new_field_kind
            child.children[0].setdefault("name", field_name)
            if field_kind in ["type", "vartype"]:
                field_type = child.children[1].children[0].astext()
                if "\n" in field_type:
                    raise InvalidRstError(
                        context.current_file,
                        "ERROR",
                        get_code_line(context.current_file, field_type),
                        "Multi-line type hints are not supported.",
                    )
                if field_kind == "type":
                    param_types[field_name] = field_type
                if field_kind == "vartype":
                    var_types[field_name] = field_type
                node.remove(child)
                continue
            if field_typing:
                already_typed.append(field_name)
            if field_kind in field_types_mapping:
                if field_kind.startswith("return") and returns_fields:
                    raise InvalidRstError(
                        context.current_file,
                        "ERROR",
                        get_code_line(context.current_file, child.astext()),
                        "Multiple `:return:` fields are not allowed. Please"
                        " combine them into one.",
                    )
                field_types_mapping[field_kind].append(child)
            else:
                other_fields.append(child)
        for fields, types, type_field_name, field_type in [
            (param_fields, param_types, "type", "param"),
            (var_fields, var_types, "vartype", "var"),
        ]:
            for field in fields:
                field_name = field.children[0].get("name")
                if field_name in already_typed and field_name in types:
                    raise InvalidRstError(
                        context.current_file,
                        "ERROR",
                        get_code_line(context.current_file, field.astext()),
                        "Type hint is specified both in the field body and in the"
                        f" `:{type_field_name}:` field. Please remove one of them.",
                    )
                else:
                    field_typing = types.get(field_name, [])
                    if field_typing:
                        field.children[0].replace_self(
                            docutils.nodes.field_name(
                                "", f"{field_type} {field_typing} {field_name}"
                            )
                        )
        yield from chain(
            self.manager.perform_format(child, context) for child in param_fields
        )
        previous_fields = param_fields
        if (
            previous_fields
            and returns_fields
            + rtype_fields
            + raises_fields
            + var_fields
            + other_fields
        ):
            yield ""
        yield from chain(
            self.manager.perform_format(child, context)
            for child in returns_fields + rtype_fields
        )
        previous_fields = returns_fields + rtype_fields
        if previous_fields and raises_fields + var_fields + other_fields:
            yield ""
        yield from chain(
            self.manager.perform_format(child, context) for child in raises_fields
        )
        previous_fields = raises_fields
        if previous_fields and var_fields + other_fields:
            yield ""
        yield from chain(
            self.manager.perform_format(child, context) for child in var_fields
        )
        previous_fields = var_fields
        if previous_fields and other_fields:
            yield ""
        yield from chain(
            self.manager.perform_format(child, context) for child in other_fields
        )

    def field_name(self, node: docutils.nodes.field_name, context) -> line_iterator:
        text = " ".join(chain(self._format_children(node, context)))
        body = ":"
        field_kinds = [
            "param",
            "raise",
            "return",
        ]
        for field_kind in field_kinds:
            if text.startswith(field_kind):
                field_kind, *_ = text.split(" ", maxsplit=1)
                body += field_kind
                text = text[len(field_kind) :]
                break
        body += text
        body += ":"
        yield body

    def footnote(self, node: docutils.nodes.footnote_reference, context):
        prefix = ".."
        children = self._wrap_text(
            (context.width - 4 if context.width is not None else None),
            chain(self._format_children(node, context.indent(4))),
            context.wrap_first_at(len(prefix) - 4).indent(4),
        )
        yield " ".join([prefix, next(children)])
        remaining = list(children)
        if remaining:
            yield from self._with_spaces(4, remaining)

    def footnote_reference(self, node: docutils.nodes.footnote_reference, context):
        if node.attributes["refname"]:
            yield f"[{node.attributes['refname']}]_"

    def inline(self, node: docutils.nodes.inline, context) -> inline_iterator:
        yield from chain(self._format_children(node, context))  # pragma: no cover

    def label(self, node: docutils.nodes.footnote_reference, context):
        yield f"[{' '.join(chain(self._format_children(node, context)))}]"

    def line(self, node: docutils.nodes.line, context) -> line_iterator:
        if not node.children:
            yield "|"
            return

        indent = 4 * context.line_block_depth
        context = context.indent(indent)
        prefix1 = f"|{' ' * (indent - 1)}"
        prefix2 = " " * indent
        for first, line in self._enum_first(
            self._wrap_text(
                context.width, chain(self._format_children(node, context)), context
            )
        ):
            yield (prefix1 if first else prefix2) + line

    def line_block(self, node: docutils.nodes.line_block, context) -> line_iterator:
        yield from chain(self._format_children(node, context.in_line_block()))

    def list_item(self, node: docutils.nodes.list_item, context) -> line_iterator:
        if not node.children:  # pragma: no cover
            yield "-"  # no idea why this isn't covered anymore
            return
        if context.current_ordinal:
            if context.bullet not in ["-", "*", "+"]:
                context.bullet = make_enumerator(
                    context.current_ordinal, context.ordinal_format, ("", ".")
                )
                context.current_ordinal += 1
        width = len(context.bullet) + 1
        bullet = f"{context.bullet} "
        spaces = " " * width
        context = context.indent(width)
        context.bullet = ""
        for first, child in self._enum_first(
            self._chain_with_line_separator("", self._format_children(node, context))
        ):
            yield ((bullet if first else spaces) if child else "") + child

    def literal(self, node: docutils.nodes.literal, context) -> inline_iterator:
        yield inline_markup(
            f"``{''.join(chain(self._format_children(node, context)))}``"
        )

    def literal_block(
        self, node: docutils.nodes.literal_block, context: FormatContext
    ) -> line_iterator:
        yield "::"
        yield from self._prepend_if_any(
            "", self._with_spaces(4, node.rawsource.splitlines())
        )

    def paragraph(
        self, node: docutils.nodes.paragraph, context: FormatContext
    ) -> line_iterator:
        wrap_text_context = context.sub_indent(context.subsequent_indent)
        if context.is_docstring:
            context.is_docstring = False
            wrap_text_context.is_docstring = False
            context = context.with_width(None)
        yield from self._wrap_text(
            context.width,
            chain(
                self._format_children(
                    node, context.sub_indent(context.subsequent_indent)
                )
            ),
            wrap_text_context,
        )

    def pending(self, node: pending, context) -> inline_iterator:  # pragma: no cover
        raise NotImplementedError("Unknown node found.")

    def problematic(
        self, node: docutils.nodes.paragraph, context
    ) -> line_iterator:  # pragma: no cover
        yield from chain(self._format_children(node, context))

    def ref_role(self, node: docutils.nodes.Node, context) -> inline_iterator:
        attributes = node.attributes
        target = attributes["target"]
        if attributes["has_explicit_title"]:
            title = attributes["title"].replace("<", r"\<")
            # TODO: This is a bit too broad, but not incorrect.
            title = title.replace("`", r"\`")
            text = f"{title} <{target}>"
        else:
            text = target
        yield inline_markup(f":{attributes['name']}:`{text}`")

    def reference(self, node: docutils.nodes.reference, context) -> inline_iterator:
        title = " ".join(
            self._wrap_text(None, chain(self._format_children(node, context)), context)
        )
        anonymous_suffix: Callable[[bool], str] = (
            lambda anonymous: "__" if anonymous else "_"
        )
        attributes = node.attributes  # type: ignore
        children = node.children  # type: ignore

        # Handle references that are also substitution references.
        if len(children) == 1 and isinstance(
            children[0], docutils.nodes.substitution_reference
        ):
            anonymous = bool(attributes.get("anonymous"))
            yield inline_markup(title + anonymous_suffix(anonymous))
            return

        # Handle references to external URIs. They can be either standalone hyperlinks,
        # written as just the URI, or an explicit "`text <url>`_" or "`text <url>`__".
        if "refuri" in attributes:
            uri = attributes["refuri"]
            if uri == title or uri == f"mailto:{title}":
                yield inline_markup(title)
            else:
                anonymous = "target" not in attributes
                yield inline_markup(f"`{title} <{uri}>`{anonymous_suffix(anonymous)}")
            return

        # Simple reference names can consist of "alphanumerics plus isolated (no two
        # adjacent) internal hyphens, underscores, periods, colons and plus signs",
        # according to
        # https://docutils.sourceforge.io/docs/ref/rst/restructuredtext.html#reference-names.
        is_single_word = re.match("^[-_.:+a-zA-Z0-9]+$", title) and not re.search(
            "[-_.:+][-_.:+]", title
        )

        # "x__" is one of the few cases to trigger an explicit "anonymous" attribute
        # (the other being the similar "|x|__", which is already handled above).
        if "anonymous" in attributes:
            if not is_single_word:
                title = f"`{title}`"
            yield inline_markup(title + anonymous_suffix(True))
            return

        anonymous = "target" not in attributes
        ref = attributes["refname"]
        # Check whether the reference name matches the text and can be made implicit.
        # (Reference names are case-insensitive.)
        if anonymous and ref.lower() == title.lower():
            if not is_single_word:
                title = f"`{title}`"
            # "x_" is equivalent to "`x <x_>`__"; it's anonymous despite having a single
            # underscore.
            yield inline_markup(title + anonymous_suffix(False))
        else:
            yield inline_markup(f"`{title} <{ref}_>`{anonymous_suffix(anonymous)}")

    def role(self, node: docutils.nodes.Node, context) -> inline_iterator:
        yield inline_markup(f":{node.attributes['role']}:`{node.attributes['text']}`")

    def row(self, node: docutils.nodes.row, context) -> line_iterator:
        all_lines = [
            self._chain_with_line_separator(
                "", self._format_children(entry, context.with_width(width))
            )
            for entry, width in zip(node.children, context.column_widths)
        ]
        for line_group in itertools.zip_longest(*all_lines):
            yield " ".join(
                (line or "").ljust(width)
                for line, width in zip(line_group, context.column_widths)
            )

    def section(self, node: docutils.nodes.section, context) -> line_iterator:
        yield from self._chain_with_line_separator(
            "", self._format_children(node, context.in_section())
        )

    def strong(self, node: docutils.nodes.strong, context) -> inline_iterator:
        joined = "".join(chain(self._format_children(node, context))).replace(
            "*", "\\*"
        )
        yield inline_markup(f"**{joined}**")

    def substitution_definition(
        self, node: docutils.nodes.substitution_reference, context
    ) -> inline_iterator:
        elements = node.rawsource.split("|")
        target = elements[1]
        directive = elements[2].strip().split("::")[0]
        prefix = f".. |{target}| {directive}::"
        children = self._wrap_text(
            (context.width - 4 if context.width is not None else None),
            chain(self._format_children(node, context.indent(4))),
            context.wrap_first_at(len(prefix) - 4).indent(4),
        )
        yield " ".join([prefix, next(children)])
        remaining = list(children)
        if remaining:
            yield from self._with_spaces(4, remaining)

    def substitution_reference(
        self, node: docutils.nodes.substitution_reference, context
    ) -> inline_iterator:
        child = chain(self._format_children(node, context))
        yield inline_markup(f"|{''.join(child)}|")

    def table(self, node: docutils.nodes.table, context) -> line_iterator:
        rows = []
        for row in node.findall(docutils.nodes.row):
            current_row = []
            for column in row.findall(docutils.nodes.entry):
                if column.attributes.get("morerows", False) or column.attributes.get(
                    "morecols", False
                ):
                    raise NotImplementedError(
                        "Tables with cells that span multiple cells are not supported."
                        " Consider using the 'include' directive to include the table"
                        " from another file."
                    )
                current_row.append(column)
            rows.append(current_row)
        column_count = len(rows[0])
        total_width = context.width - column_count + 1
        table_matrix_min = self._generate_table_matrix(context, rows, 1)
        table_matrix_max = self._generate_table_matrix(context, rows, total_width)
        min_col_len = {
            col_index: max([row[col_index] for row in table_matrix_min])
            for col_index in range(column_count)
        }
        max_col_len = {
            col_index: max([row[col_index] for row in table_matrix_max])
            for col_index in range(column_count)
        }
        column_lengths = {}
        current_width = 0
        if total_width is None or sum(max_col_len.values()) <= total_width:
            final_widths = [
                max(
                    self._generate_table_matrix(context, rows, None, max_col_len),
                    key=lambda lengths: lengths[i],
                )[i]
                for i in range(column_count)
            ]
            context = context.with_column_widths(final_widths)
        else:
            for column_progress, column_info in enumerate(
                sorted(max_col_len.items(), key=lambda item: item[1])
            ):
                column_index, column_width = column_info
                if column_index not in column_lengths:
                    if (current_width + column_width) <= total_width:
                        current_width += column_width
                        column_lengths[column_index] = column_width
                    else:
                        proposed_column_length = self._divide_evenly(
                            total_width - current_width, column_count - column_progress
                        ).pop()
                        if (column_width < proposed_column_length) or (
                            column_width < 25
                        ):
                            column_lengths[column_index] = column_width
                        elif proposed_column_length >= min_col_len[column_index]:
                            if proposed_column_length < 25:
                                column_lengths[column_index] = 25
                            else:
                                column_lengths[column_index] = proposed_column_length
                        else:
                            column_lengths[column_index] = column_width
            final_widths = [
                max(
                    self._generate_table_matrix(context, rows, None, column_lengths),
                    key=lambda lengths: lengths[i],
                )[i]
                for i in range(column_count)
            ]
            context = context.with_column_widths(final_widths)
        yield from [
            line.rstrip(" ")
            for line in self._chain_with_line_separator(
                "", self._format_children(node, context)
            )
        ]

    def target(self, node: docutils.nodes.target, context) -> line_iterator:
        # if not isinstance(node.parent, (docutils.nodes.document, docutils.nodes.section)):
        #     return
        if not node.rawsource.startswith(".. _"):
            return
        try:
            body = f" {node.attributes['refuri']}"
        except KeyError:
            body = (
                f" {node.attributes['refname']}_"
                if "refname" in node.attributes
                else ""
            )

        name = "_" if node.attributes.get("anonymous") else node.attributes["names"][0]
        yield f".. _{name}:{body}"

    def tbody(self, node: docutils.nodes.tbody, context) -> line_iterator:
        yield from chain(self._format_children(node, context))

    thead = tbody

    def term(self, node: docutils.nodes.term, context) -> line_iterator:
        yield " ".join(
            self._wrap_text(None, chain(self._format_children(node, context)), context)
        )

    def Text(self, node: docutils.nodes.Text, _) -> inline_iterator:
        yield unescape(node, restore_backslashes=True).replace(r"\ ", "")

    def tgroup(
        self, node: docutils.nodes.tgroup, context: FormatContext
    ) -> line_iterator:
        sep = " ".join("=" * width for width in context.column_widths)
        yield sep
        for child in node.children:
            if isinstance(child, docutils.nodes.colspec):
                continue
            if isinstance(child, docutils.nodes.thead):
                yield from self.manager.perform_format(child, context)
                yield " ".join("=" * width for width in context.column_widths)
            if isinstance(child, docutils.nodes.tbody):
                yield from self.manager.perform_format(child, context)
                yield sep

    def title(self, node: docutils.nodes.title, context) -> line_iterator:
        text = " ".join(
            self._wrap_text(None, chain(self._format_children(node, context)), context)
        )
        char = section_chars[context.section_depth - 1]
        yield text
        yield char * len(text)

    def title_reference(
        self, node: docutils.nodes.title_reference, context
    ) -> inline_iterator:
        yield inline_markup(f"`{''.join(chain(self._format_children(node, context)))}`")

    def transition(self, node: docutils.nodes.transition, context) -> line_iterator:
        yield "----"


class IgnoreMessagesReporter(Reporter):
    """A Docutils error reporter that ignores some messages.

    We want to handle most system messages normally, but it's useful to ignore some (and
    just doing it by level would be too coarse). In particular, having too short a title
    line leads to a warning but parses just fine; ignoring that message means we can
    automatically fix lengths whether they're too short or too long (though they do have
    to be at least four characters to be parsed correctly in the first place).

    """

    ignored_messages = {"Title overline too short.", "Title underline too short."}

    def system_message(
        self, level: int, message: str, *children: Any, **kwargs: Any
    ) -> docutils.nodes.system_message:  # pragma: no cover
        orig_level = self.halt_level
        if message in self.ignored_messages:
            self.halt_level = Reporter.SEVERE_LEVEL + 1
        msg = super().system_message(level, message, *children, **kwargs)
        self.halt_level = orig_level
        return msg


class Manager:
    def __init__(self, reporter, black_config=None, docstring_trailing_line=True):
        rst_extras.register()
        self.black_config = black_config
        self.error_count = 0
        self.reporter = reporter
        self.settings = OptionParser(components=[rst.Parser]).get_default_values()
        self.settings.smart_quotes = True
        self.settings.report_level = 5
        self.settings.halt_level = 5
        self.settings.file_insertion_enabled = False
        self.settings.tab_width = 8
        self.formatters = Formatters(self)
        self.current_file = None
        self.docstring_trailing_line = docstring_trailing_line

    def _pre_process(self, node: docutils.nodes.Node, source: str) -> None:
        """Preprocess nodes.

        This does some preprocessing to all nodes that is generic across node types and
        is therefore most convenient to do as a simple recursive function rather than as
        part of the big dispatcher class.

        """
        # Strip all system_message nodes. (Just formatting them with no markup isn't enough, since that
        # could lead to extra spaces or empty lines between other elements.)
        errors = [
            child
            for child in node.children
            if isinstance(child, docutils.nodes.system_message)
            and child.attributes["type"] != "INFO"
        ]
        if errors:
            self.error_count += len(errors)
            raise InvalidRstErrors(
                [
                    InvalidRstError(
                        self.current_file,
                        error.attributes["type"],
                        error.line,
                        error.children[0].children[0],
                    )
                    for error in errors
                ]
            )
        node.children = [
            child
            for child in node.children
            if not isinstance(child, docutils.nodes.system_message)
        ]

        # Match references to targets, which helps later with distinguishing whether they're anonymous.
        for reference, target in pairwise(node.children):
            if isinstance(reference, docutils.nodes.reference) and isinstance(
                target, docutils.nodes.target
            ):
                reference.attributes["target"] = target
        start = None
        for i, child in enumerate(itertools.chain(node.children, [None])):
            in_run = start is not None
            is_target = isinstance(child, docutils.nodes.target)
            if in_run and not is_target:
                # Anonymous targets have a value of `[]` for "names", which will sort to the top. Also,
                # it's important here that `sorted` is stable, or anonymous targets could break.
                node.children[start:i] = sorted(
                    node.children[start:i], key=lambda t: t.attributes["names"]
                )
                start = None
            elif not in_run and is_target:
                start = i

        # Recurse.
        for child in node.children:
            self._pre_process(child, source)

    def format_node(self, width, node: docutils.nodes.Node, is_docstring=False) -> str:
        formatted_node = "\n".join(
            self.perform_format(
                node,
                FormatContext(
                    width,
                    current_file=self.current_file,
                    manager=self,
                    black_config=self.black_config,
                    is_docstring=is_docstring,
                ),
            )
        )
        return f"{formatted_node}\n"

    def parse_string(self, file_name: str, text: str) -> docutils.nodes.document:
        self.current_file = file_name
        doc = new_document(str(self.current_file), self.settings)
        parser = rst.Parser()
        parser.parse(text, doc)
        doc.reporter = IgnoreMessagesReporter(
            "", self.settings.report_level, self.settings.halt_level
        )
        self._pre_process(doc, text)
        return doc

    def perform_format(self, node: docutils.nodes.Node, context) -> Iterator[str]:
        try:
            func = getattr(self.formatters, type(node).__name__)
        except AttributeError:  # pragma: no cover
            raise ValueError(f"Unknown node type {type(node).__name__}!")
        return func(node, context)
