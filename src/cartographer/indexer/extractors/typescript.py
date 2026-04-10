"""TypeScript/JavaScript extractor using tree-sitter."""

from __future__ import annotations

import tree_sitter_typescript as tstypescript
import tree_sitter_javascript as tsjavascript
from tree_sitter import Language, Parser, Node

from .base import SymbolNode, EdgeRef, FileExtraction

TS_LANGUAGE = Language(tstypescript.language_typescript())
TSX_LANGUAGE = Language(tstypescript.language_tsx())
JS_LANGUAGE = Language(tsjavascript.language())

# Builtin/primitive types to skip in EXPECTS/RETURNS edges
_BUILTIN_TYPES = frozenset({
    "string", "number", "boolean", "void", "null", "undefined", "never",
    "any", "unknown", "object", "symbol", "bigint",
})


class TypeScriptExtractor:
    lang = "typescript"
    extensions = (".ts",)

    def __init__(self) -> None:
        self._parser = Parser(TS_LANGUAGE)

    def extract(self, file_path: str, source: bytes) -> FileExtraction:
        tree = self._parser.parse(source)
        ctx = _ExtractionContext(file_path, source)
        ctx.visit(tree.root_node)
        return FileExtraction(file_path=file_path, lang=self.lang, nodes=ctx.nodes, edges=ctx.edges)


class TSXExtractor:
    lang = "tsx"
    extensions = (".tsx",)

    def __init__(self) -> None:
        self._parser = Parser(TSX_LANGUAGE)

    def extract(self, file_path: str, source: bytes) -> FileExtraction:
        tree = self._parser.parse(source)
        ctx = _ExtractionContext(file_path, source)
        ctx.visit(tree.root_node)
        return FileExtraction(file_path=file_path, lang=self.lang, nodes=ctx.nodes, edges=ctx.edges)


class JavaScriptExtractor:
    lang = "javascript"
    extensions = (".js", ".jsx", ".mjs", ".cjs")

    def __init__(self) -> None:
        self._parser = Parser(JS_LANGUAGE)

    def extract(self, file_path: str, source: bytes) -> FileExtraction:
        tree = self._parser.parse(source)
        ctx = _ExtractionContext(file_path, source)
        ctx.visit(tree.root_node)
        return FileExtraction(file_path=file_path, lang=self.lang, nodes=ctx.nodes, edges=ctx.edges)


class _ExtractionContext:
    """Walks a TS/JS tree-sitter AST and collects symbols + edges."""

    def __init__(self, file_path: str, source: bytes) -> None:
        self.file_path = file_path
        self.source = source
        self.nodes: list[SymbolNode] = []
        self.edges: list[EdgeRef] = []
        self._scope: list[str] = []
        self._exported_names: set[str] = set()

    def _text(self, node: Node) -> str:
        return node.text.decode("utf-8") if node.text else ""

    def _make_id(self, name: str) -> str:
        if self._scope:
            return f"{self._scope[-1]}::{name}"
        return f"{self.file_path}::{name}"

    def _current_class_id(self) -> str | None:
        for sid in reversed(self._scope):
            if any(n.kind == "class" and n.id == sid for n in self.nodes):
                return sid
        return None

    def _current_function_id(self) -> str | None:
        for sid in reversed(self._scope):
            if any(n.kind == "function" and n.id == sid for n in self.nodes):
                return sid
        return None

    def _is_exported(self, node: Node) -> bool:
        """Check if the node's parent is an export_statement."""
        return node.parent is not None and node.parent.type == "export_statement"

    # -- visitor dispatch -----------------------------------------------------

    def visit(self, node: Node) -> None:
        handler = _VISITORS.get(node.type)
        if handler:
            handler(self, node)
        else:
            for child in node.children:
                self.visit(child)

    # -- imports --------------------------------------------------------------

    def _visit_import(self, node: Node) -> None:
        """Extract import edges from: import ... from 'source'"""
        source_node = node.child_by_field_name("source")
        if not source_node:
            return
        # Get the string content (strip quotes)
        source_text = self._text(source_node).strip("\"'`")
        self.edges.append(EdgeRef(
            "IMPORTS", "File", self.file_path, "File", source_text,
            resolved=False,
        ))

    # -- classes --------------------------------------------------------------

    def _visit_class(self, node: Node) -> None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        name = self._text(name_node)
        class_id = self._make_id(name)
        exported = self._is_exported(node) or name in self._exported_names
        visibility = "public" if exported else "private"

        self.nodes.append(SymbolNode(
            kind="class",
            id=class_id,
            name=name,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            visibility=visibility,
        ))
        self.edges.append(EdgeRef("CONTAINS_CLASS", "File", self.file_path, "Class", class_id))

        # Heritage clause: extends / implements
        # TS uses "class_heritage" with extends_clause/implements_clause children
        # JS uses "class_heritage" with direct "extends" keyword + identifier
        for child in node.children:
            if child.type == "class_heritage":
                self._visit_heritage(child, class_id)

        # Visit body
        body = node.child_by_field_name("body")
        if body:
            self._scope.append(class_id)
            for child in body.children:
                self.visit(child)
            self._scope.pop()

    def _visit_heritage(self, node: Node, class_id: str) -> None:
        """Parse `extends Foo` and `implements Bar, Baz`.

        TS: class_heritage > extends_clause > type_identifier
        JS: class_heritage > extends (keyword) + identifier
        """
        for child in node.children:
            if child.type == "extends_clause":
                for val in child.children:
                    if val.type in ("identifier", "type_identifier"):
                        self.edges.append(EdgeRef(
                            "INHERITS", "Class", class_id, "Class", self._text(val),
                            resolved=False,
                        ))
            elif child.type == "implements_clause":
                for val in child.children:
                    if val.type in ("identifier", "type_identifier"):
                        self.edges.append(EdgeRef(
                            "INHERITS", "Class", class_id, "Class", self._text(val),
                            resolved=False,
                        ))
            elif child.type == "identifier":
                # JS direct: class Foo extends Bar — "Bar" is a direct identifier child
                self.edges.append(EdgeRef(
                    "INHERITS", "Class", class_id, "Class", self._text(child),
                    resolved=False,
                ))

    # -- functions / methods --------------------------------------------------

    def _visit_function_declaration(self, node: Node) -> None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        name = self._text(name_node)
        self._register_function(node, name, is_method=False)

    def _visit_method_definition(self, node: Node) -> None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        name = self._text(name_node)
        self._register_function(node, name, is_method=True)

    def _visit_lexical_declaration(self, node: Node) -> None:
        """Handle `const foo = (...) => ...` arrow functions and module-level variables."""
        for child in node.children:
            if child.type == "variable_declarator":
                self._visit_variable_declarator(child, exported=self._is_exported(node))

    def _visit_variable_statement(self, node: Node) -> None:
        """Handle `var foo = ...` style declarations."""
        for child in node.children:
            if child.type == "variable_declarator":
                self._visit_variable_declarator(child, exported=self._is_exported(node))

    def _visit_variable_declarator(self, node: Node, exported: bool = False) -> None:
        name_node = node.child_by_field_name("name")
        value_node = node.child_by_field_name("value")
        if not name_node or name_node.type != "identifier":
            return

        name = self._text(name_node)

        # Is the value an arrow function?
        if value_node and value_node.type == "arrow_function":
            self._register_function(value_node, name, is_method=False, decl_node=node)
            return

        # Is the value a regular function expression?
        if value_node and value_node.type == "function_expression":
            self._register_function(value_node, name, is_method=False, decl_node=node)
            return

        # Otherwise it's a module-level variable (only if top-level)
        if not self._scope:
            var_id = self._make_id(name)
            self.nodes.append(SymbolNode(
                kind="variable",
                id=var_id,
                name=name,
                file_path=self.file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                visibility="public" if exported else "private",
            ))
            self.edges.append(EdgeRef("CONTAINS_VAR", "File", self.file_path, "Variable", var_id))

        # Always recurse into value to find nested calls (e.g., const x = this.method())
        if value_node:
            self.visit(value_node)

    def _register_function(
        self, node: Node, name: str, is_method: bool, decl_node: Node | None = None,
    ) -> None:
        func_id = self._make_id(name)
        parent_class = self._current_class_id()
        if parent_class:
            is_method = True

        # Determine visibility
        exported = self._is_exported(node) or self._is_exported(decl_node) if decl_node else self._is_exported(node)
        if is_method:
            # Check for private/protected/public modifiers
            visibility = "public"
            for child in node.children:
                if child.type == "accessibility_modifier":
                    visibility = self._text(child)
                    break
            if name.startswith("#"):
                visibility = "private"
        else:
            visibility = "public" if exported or name in self._exported_names else "private"

        sig = self._extract_signature(node, name)

        self.nodes.append(SymbolNode(
            kind="function",
            id=func_id,
            name=name,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            signature=sig,
            visibility=visibility,
            is_method=is_method,
            parent_id=parent_class,
        ))

        if parent_class:
            self.edges.append(EdgeRef("HAS_METHOD", "Class", parent_class, "Function", func_id))
        else:
            self.edges.append(EdgeRef("CONTAINS_FUNC", "File", self.file_path, "Function", func_id))

        # Extract type annotations
        self._extract_type_annotations(node, func_id)

        # Visit body for calls
        body = node.child_by_field_name("body")
        if body:
            self._scope.append(func_id)
            for child in body.children:
                self.visit(child)
            self._scope.pop()

    # -- interfaces and type aliases ------------------------------------------

    def _visit_interface(self, node: Node) -> None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        name = self._text(name_node)
        type_id = self._make_id(name)
        exported = self._is_exported(node)

        self.nodes.append(SymbolNode(
            kind="type",
            id=type_id,
            name=name,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            visibility="public" if exported else "private",
        ))
        self.edges.append(EdgeRef("CONTAINS_TYPE", "File", self.file_path, "Type", type_id))

        # Interface can extend other interfaces
        for child in node.children:
            if child.type == "extends_type_clause":
                for t in child.children:
                    if t.type in ("type_identifier", "identifier"):
                        self.edges.append(EdgeRef(
                            "INHERITS", "Class", type_id, "Class", self._text(t),
                            resolved=False,
                        ))

    def _visit_type_alias(self, node: Node) -> None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        name = self._text(name_node)
        type_id = self._make_id(name)
        exported = self._is_exported(node)

        self.nodes.append(SymbolNode(
            kind="type",
            id=type_id,
            name=name,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            visibility="public" if exported else "private",
        ))
        self.edges.append(EdgeRef("CONTAINS_TYPE", "File", self.file_path, "Type", type_id))

    # -- calls ----------------------------------------------------------------

    def _visit_call(self, node: Node) -> None:
        enclosing_func = self._current_function_id()
        if not enclosing_func:
            return

        func_node = node.child_by_field_name("function")
        if not func_node:
            return

        if func_node.type == "identifier":
            callee_name = self._text(func_node)
            self.edges.append(EdgeRef(
                "CALLS", "Function", enclosing_func, "Function", callee_name,
                resolved=False,
            ))
        elif func_node.type == "member_expression":
            prop = func_node.child_by_field_name("property")
            if prop:
                callee_name = self._text(prop)
                self.edges.append(EdgeRef(
                    "CALLS", "Function", enclosing_func, "Function", callee_name,
                    resolved=False,
                ))

        # Recurse into arguments for nested calls
        args = node.child_by_field_name("arguments")
        if args:
            for child in args.children:
                self.visit(child)

    # -- new expressions ------------------------------------------------------

    def _visit_new(self, node: Node) -> None:
        """Handle `new ClassName(...)` as a call."""
        enclosing_func = self._current_function_id()
        if not enclosing_func:
            return

        constructor = node.child_by_field_name("constructor")
        if constructor and constructor.type == "identifier":
            callee_name = self._text(constructor)
            self.edges.append(EdgeRef(
                "CALLS", "Function", enclosing_func, "Function", callee_name,
                resolved=False,
            ))

        for child in node.children:
            if child.type not in ("identifier", "new"):
                self.visit(child)

    # -- export statement -----------------------------------------------------

    def _visit_export(self, node: Node) -> None:
        """Handle export statements — visit the inner declaration."""
        for child in node.children:
            if child.type in ("function_declaration", "class_declaration",
                              "interface_declaration", "type_alias_declaration",
                              "lexical_declaration", "variable_declaration"):
                self.visit(child)
            elif child.type == "identifier":
                self._exported_names.add(self._text(child))

    # -- type annotation helpers ----------------------------------------------

    def _extract_signature(self, node: Node, name: str) -> str:
        """Build a human-readable signature string."""
        params_node = node.child_by_field_name("parameters")
        if not params_node:
            return name

        params_text = self._text(params_node)
        # Check for async
        is_async = any(c.type == "async" for c in node.children)
        prefix = "async " if is_async else ""

        # Return type
        return_type = ""
        for child in node.children:
            if child.type == "type_annotation" and child.prev_sibling == params_node:
                return_type = self._text(child)
                break

        return f"{prefix}{name}{params_text}{return_type}"

    def _extract_type_annotations(self, func_node: Node, func_id: str) -> None:
        """Extract EXPECTS/RETURNS edges from TypeScript type annotations."""
        params_node = func_node.child_by_field_name("parameters")
        if params_node:
            for param in params_node.children:
                if param.type in ("required_parameter", "optional_parameter"):
                    type_ann = param.child_by_field_name("type")
                    if not type_ann:
                        for c in param.children:
                            if c.type == "type_annotation":
                                type_ann = c
                                break
                    if type_ann:
                        type_name = self._extract_type_name(type_ann)
                        if type_name and type_name.lower() not in _BUILTIN_TYPES:
                            self.edges.append(EdgeRef(
                                "EXPECTS_TYPE", "Function", func_id, "Type", type_name,
                                resolved=False,
                            ))

        # Return type: use the return_type field (works for function_declaration,
        # method_definition) or scan for type_annotation after params (arrow_function)
        return_ann = func_node.child_by_field_name("return_type")
        if not return_ann and params_node:
            # Arrow functions don't have a return_type field — find the type_annotation
            # that comes after the parameters
            for child in func_node.children:
                if child.type == "type_annotation" and child.start_point > params_node.end_point:
                    return_ann = child
                    break

        if return_ann:
            type_name = self._extract_type_name(return_ann)
            if type_name and type_name.lower() not in _BUILTIN_TYPES:
                self.edges.append(EdgeRef(
                    "RETURNS_TYPE", "Function", func_id, "Type", type_name,
                    resolved=False,
                ))

    def _extract_type_name(self, node: Node) -> str | None:
        """Extract a usable type name from a type annotation node.

        Unwraps Promise<T> → T, handles generic_type, type_identifier, etc.
        """
        # Walk down to find the core type
        for child in node.children:
            if child.type == "type_identifier":
                return self._text(child)
            elif child.type == "generic_type":
                # e.g., Promise<Receipt> — extract the inner type for Promise
                type_name_node = child.child_by_field_name("name")
                if type_name_node:
                    name = self._text(type_name_node)
                    if name == "Promise":
                        # Unwrap Promise<T> to get T
                        args = child.child_by_field_name("type_arguments")
                        if args:
                            for arg in args.children:
                                if arg.type in ("type_identifier", "generic_type"):
                                    inner = self._extract_type_name_from_node(arg)
                                    if inner:
                                        return inner
                    return name
            elif child.type == "predefined_type":
                return self._text(child)
        return None

    def _extract_type_name_from_node(self, node: Node) -> str | None:
        """Extract type name directly from a type node (not annotation wrapper)."""
        if node.type == "type_identifier":
            return self._text(node)
        elif node.type == "generic_type":
            name_node = node.child_by_field_name("name")
            return self._text(name_node) if name_node else None
        return None


# Visitor dispatch table
_VISITORS: dict[str, object] = {
    "import_statement": _ExtractionContext._visit_import,
    "class_declaration": _ExtractionContext._visit_class,
    "function_declaration": _ExtractionContext._visit_function_declaration,
    "method_definition": _ExtractionContext._visit_method_definition,
    "lexical_declaration": _ExtractionContext._visit_lexical_declaration,
    "variable_declaration": _ExtractionContext._visit_variable_statement,
    "interface_declaration": _ExtractionContext._visit_interface,
    "type_alias_declaration": _ExtractionContext._visit_type_alias,
    "call_expression": _ExtractionContext._visit_call,
    "new_expression": _ExtractionContext._visit_new,
    "export_statement": _ExtractionContext._visit_export,
}
