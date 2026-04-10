"""Python language extractor using tree-sitter."""

from __future__ import annotations

import tree_sitter_python as tspython
from tree_sitter import Language, Parser, Node

from .base import SymbolNode, EdgeRef, FileExtraction

PY_LANGUAGE = Language(tspython.language())


class PythonExtractor:
    lang = "python"
    extensions = (".py",)

    def __init__(self) -> None:
        self._parser = Parser(PY_LANGUAGE)

    def extract(self, file_path: str, source: bytes) -> FileExtraction:
        tree = self._parser.parse(source)
        ctx = _ExtractionContext(file_path, source)
        ctx.visit(tree.root_node)
        return FileExtraction(
            file_path=file_path,
            lang=self.lang,
            nodes=ctx.nodes,
            edges=ctx.edges,
        )


class _ExtractionContext:
    """Walks a Python tree-sitter AST and collects symbols + edges."""

    def __init__(self, file_path: str, source: bytes) -> None:
        self.file_path = file_path
        self.source = source
        self.nodes: list[SymbolNode] = []
        self.edges: list[EdgeRef] = []
        self._scope: list[str] = []  # stack of parent class/function ids

    # -- helpers --------------------------------------------------------------

    def _text(self, node: Node) -> str:
        return node.text.decode("utf-8") if node.text else ""

    def _make_id(self, name: str) -> str:
        if self._scope:
            return f"{self._scope[-1]}::{name}"
        return f"{self.file_path}::{name}"

    def _current_class_id(self) -> str | None:
        """Return the nearest enclosing class id, if any."""
        for sid in reversed(self._scope):
            # class ids don't contain ".<method>" patterns from functions
            if any(n.kind == "class" and n.id == sid for n in self.nodes):
                return sid
        return None

    def _current_function_id(self) -> str | None:
        """Return the nearest enclosing function id, if any."""
        for sid in reversed(self._scope):
            if any(n.kind == "function" and n.id == sid for n in self.nodes):
                return sid
        return None

    # -- visitor --------------------------------------------------------------

    def visit(self, node: Node) -> None:
        if node.type == "function_definition":
            self._visit_function(node)
        elif node.type == "class_definition":
            self._visit_class(node)
        elif node.type in ("import_statement", "import_from_statement"):
            self._visit_import(node)
        elif node.type == "assignment":
            self._visit_assignment(node)
        elif node.type == "call":
            self._visit_call(node)
        else:
            for child in node.children:
                self.visit(child)

    def _visit_function(self, node: Node) -> None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        name = self._text(name_node)
        func_id = self._make_id(name)
        parent_class = self._current_class_id()
        is_method = parent_class is not None

        # Build signature from parameters and return type
        sig = self._extract_signature(node, name)

        # Visibility heuristic
        visibility = "private" if name.startswith("_") and not name.startswith("__") else "public"

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

        # File CONTAINS_FUNC or Class HAS_METHOD
        if parent_class:
            self.edges.append(EdgeRef("HAS_METHOD", "Class", parent_class, "Function", func_id))
        else:
            self.edges.append(EdgeRef("CONTAINS_FUNC", "File", self.file_path, "Function", func_id))

        # Extract parameter types (EXPECTS) and return type (RETURNS)
        self._extract_type_annotations(node, func_id)

        # Visit body for calls, etc.
        self._scope.append(func_id)
        body = node.child_by_field_name("body")
        if body:
            for child in body.children:
                self.visit(child)
        self._scope.pop()

    def _visit_class(self, node: Node) -> None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        name = self._text(name_node)
        class_id = self._make_id(name)

        self.nodes.append(SymbolNode(
            kind="class",
            id=class_id,
            name=name,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            visibility="public" if not name.startswith("_") else "private",
        ))

        self.edges.append(EdgeRef("CONTAINS_CLASS", "File", self.file_path, "Class", class_id))

        # Inheritance: superclasses in argument_list
        superclasses = node.child_by_field_name("superclasses")
        if superclasses:
            for arg in superclasses.children:
                if arg.type == "identifier":
                    base_name = self._text(arg)
                    self.edges.append(EdgeRef(
                        "INHERITS", "Class", class_id, "Class", base_name,
                        resolved=False,
                    ))
                elif arg.type == "attribute":
                    base_name = self._text(arg)
                    self.edges.append(EdgeRef(
                        "INHERITS", "Class", class_id, "Class", base_name,
                        resolved=False,
                    ))

        # Visit body
        self._scope.append(class_id)
        body = node.child_by_field_name("body")
        if body:
            for child in body.children:
                self.visit(child)
        self._scope.pop()

    def _visit_import(self, node: Node) -> None:
        """Extract file-level import edges.

        We record the module name as an unresolved reference — the indexer
        resolves these to actual File nodes later.
        """
        if node.type == "import_from_statement":
            module_node = node.child_by_field_name("module_name")
            if module_node:
                module_name = self._text(module_node)
                self.edges.append(EdgeRef(
                    "IMPORTS", "File", self.file_path, "File", module_name,
                    resolved=False,
                ))
        elif node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    module_name = self._text(child)
                    self.edges.append(EdgeRef(
                        "IMPORTS", "File", self.file_path, "File", module_name,
                        resolved=False,
                    ))
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        module_name = self._text(name_node)
                        self.edges.append(EdgeRef(
                            "IMPORTS", "File", self.file_path, "File", module_name,
                            resolved=False,
                        ))

    def _visit_assignment(self, node: Node) -> None:
        """Extract module-level variable assignments and recurse into RHS for calls."""
        # Only capture variables for top-level assignments
        if not self._scope:
            left = node.child_by_field_name("left")
            if left and left.type == "identifier":
                name = self._text(left)
                var_id = self._make_id(name)

                self.nodes.append(SymbolNode(
                    kind="variable",
                    id=var_id,
                    name=name,
                    file_path=self.file_path,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                ))
                self.edges.append(EdgeRef("CONTAINS_VAR", "File", self.file_path, "Variable", var_id))

        # Always recurse into children to find nested calls (e.g., x = self.method())
        for child in node.children:
            self.visit(child)

    def _visit_call(self, node: Node) -> None:
        """Extract function call edges."""
        enclosing_func = self._current_function_id()
        if not enclosing_func:
            return  # top-level calls are less interesting for the graph

        func_node = node.child_by_field_name("function")
        if not func_node:
            return

        if func_node.type == "identifier":
            callee_name = self._text(func_node)
            self.edges.append(EdgeRef(
                "CALLS", "Function", enclosing_func, "Function", callee_name,
                resolved=False,
            ))
        elif func_node.type == "attribute":
            # e.g., self.method() or obj.method()
            attr = func_node.child_by_field_name("attribute")
            if attr:
                callee_name = self._text(attr)
                self.edges.append(EdgeRef(
                    "CALLS", "Function", enclosing_func, "Function", callee_name,
                    resolved=False,
                ))

        # Recurse into call arguments (they may contain nested calls)
        for child in node.children:
            if child.type != "identifier" and child.type != "attribute":
                self.visit(child)

    # -- type annotation helpers ----------------------------------------------

    def _extract_signature(self, func_node: Node, name: str) -> str:
        """Build a human-readable signature string."""
        params_node = func_node.child_by_field_name("parameters")
        return_type = func_node.child_by_field_name("return_type")

        params_text = self._text(params_node) if params_node else "()"
        sig = f"def {name}{params_text}"
        if return_type:
            sig += f" -> {self._text(return_type)}"
        return sig

    def _extract_type_annotations(self, func_node: Node, func_id: str) -> None:
        """Extract EXPECTS edges from parameter type annotations and RETURNS from return type."""
        params_node = func_node.child_by_field_name("parameters")
        if params_node:
            for param in params_node.children:
                if param.type in ("typed_parameter", "typed_default_parameter"):
                    type_node = param.child_by_field_name("type")
                    if type_node:
                        type_name = self._text(type_node)
                        # Skip builtins and self
                        if type_name not in ("self", "cls", "int", "str", "float", "bool", "None", "bytes", "dict", "list", "tuple", "set"):
                            self.edges.append(EdgeRef(
                                "EXPECTS_TYPE", "Function", func_id, "Type", type_name,
                                resolved=False,
                            ))

        return_node = func_node.child_by_field_name("return_type")
        if return_node:
            type_name = self._text(return_node)
            if type_name not in ("None", "int", "str", "float", "bool", "bytes", "dict", "list", "tuple", "set"):
                self.edges.append(EdgeRef(
                    "RETURNS_TYPE", "Function", func_id, "Type", type_name,
                    resolved=False,
                ))
