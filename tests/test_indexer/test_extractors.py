"""Tests for language extractors."""

import pytest
from cartographer.indexer.extractors.python import PythonExtractor
from cartographer.indexer.extractors.typescript import (
    TypeScriptExtractor,
    JavaScriptExtractor,
    TSXExtractor,
)


# ---------------------------------------------------------------------------
# Python extractor
# ---------------------------------------------------------------------------

class TestPythonExtractor:
    def setup_method(self):
        self.ext = PythonExtractor()

    def test_extracts_functions(self):
        source = b"def hello(name: str) -> str:\n    return f'Hello {name}'"
        result = self.ext.extract("test.py", source)
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert len(funcs) == 1
        assert funcs[0].name == "hello"
        assert "def hello(name: str) -> str" in funcs[0].signature

    def test_extracts_classes_and_methods(self):
        source = b"class Foo:\n    def bar(self) -> None:\n        pass"
        result = self.ext.extract("test.py", source)
        classes = [n for n in result.nodes if n.kind == "class"]
        methods = [n for n in result.nodes if n.kind == "function" and n.is_method]
        assert len(classes) == 1
        assert classes[0].name == "Foo"
        assert len(methods) == 1
        assert methods[0].name == "bar"

    def test_extracts_inheritance(self):
        source = b"class Child(Parent):\n    pass"
        result = self.ext.extract("test.py", source)
        inherits = [e for e in result.edges if e.kind == "INHERITS"]
        assert len(inherits) == 1
        assert inherits[0].to_id == "Parent"

    def test_extracts_calls(self):
        source = b"def foo():\n    bar()\n    baz()"
        result = self.ext.extract("test.py", source)
        calls = [e for e in result.edges if e.kind == "CALLS"]
        callee_names = {e.to_id for e in calls}
        assert "bar" in callee_names
        assert "baz" in callee_names

    def test_extracts_imports(self):
        source = b"import os\nfrom pathlib import Path\nfrom .utils import helper"
        result = self.ext.extract("test.py", source)
        imports = [e for e in result.edges if e.kind == "IMPORTS"]
        sources = {e.to_id for e in imports}
        assert "os" in sources
        assert "pathlib" in sources
        assert ".utils" in sources

    def test_extracts_expects_returns(self):
        source = b"def process(order: Order) -> Receipt:\n    return Receipt()"
        result = self.ext.extract("test.py", source)
        expects = [e for e in result.edges if "EXPECTS" in e.kind]
        returns = [e for e in result.edges if "RETURNS" in e.kind]
        assert any(e.to_id == "Order" for e in expects)
        assert any(e.to_id == "Receipt" for e in returns)

    def test_extracts_module_variables(self):
        source = b"MAX_RETRIES = 3\nDEBUG = True"
        result = self.ext.extract("test.py", source)
        vars_ = [n for n in result.nodes if n.kind == "variable"]
        names = {v.name for v in vars_}
        assert "MAX_RETRIES" in names
        assert "DEBUG" in names

    def test_extracts_self_method_calls(self):
        source = b"""
class Manager:
    def process(self):
        pass
    def run(self):
        result = self.process()
        return result
"""
        result = self.ext.extract("test.py", source)
        calls = [e for e in result.edges if e.kind == "CALLS"]
        assert any(e.to_id == "process" and "run" in e.from_id for e in calls)

    def test_extracts_calls_in_assignments(self):
        source = b"""
def outer():
    x = helper()
    return x

def helper():
    pass
"""
        result = self.ext.extract("test.py", source)
        calls = [e for e in result.edges if e.kind == "CALLS"]
        assert any(e.to_id == "helper" and "outer" in e.from_id for e in calls)


# ---------------------------------------------------------------------------
# TypeScript extractor
# ---------------------------------------------------------------------------

class TestTypeScriptExtractor:
    def setup_method(self):
        self.ext = TypeScriptExtractor()

    def test_extracts_functions(self):
        source = b"export function greet(name: string): string { return name; }"
        result = self.ext.extract("test.ts", source)
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert len(funcs) == 1
        assert funcs[0].name == "greet"
        assert funcs[0].visibility == "public"

    def test_extracts_arrow_functions(self):
        source = b"const add = (a: number, b: number): number => a + b;"
        result = self.ext.extract("test.ts", source)
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert len(funcs) == 1
        assert funcs[0].name == "add"

    def test_extracts_classes_with_heritage(self):
        source = b"class Dog extends Animal implements Pet { bark() { } }"
        result = self.ext.extract("test.ts", source)
        classes = [n for n in result.nodes if n.kind == "class"]
        inherits = [e for e in result.edges if e.kind == "INHERITS"]
        assert len(classes) == 1
        assert classes[0].name == "Dog"
        base_names = {e.to_id for e in inherits}
        assert "Animal" in base_names
        assert "Pet" in base_names

    def test_extracts_interfaces(self):
        source = b"export interface Config { port: number; host: string; }"
        result = self.ext.extract("test.ts", source)
        types = [n for n in result.nodes if n.kind == "type"]
        assert len(types) == 1
        assert types[0].name == "Config"

    def test_extracts_type_aliases(self):
        source = b'type Status = "active" | "inactive";'
        result = self.ext.extract("test.ts", source)
        types = [n for n in result.nodes if n.kind == "type"]
        assert len(types) == 1
        assert types[0].name == "Status"

    def test_extracts_imports(self):
        source = b'import { Foo } from "./foo";\nimport express from "express";'
        result = self.ext.extract("test.ts", source)
        imports = [e for e in result.edges if e.kind == "IMPORTS"]
        sources = {e.to_id for e in imports}
        assert "./foo" in sources
        assert "express" in sources

    def test_extracts_calls(self):
        source = b"function foo() { bar(); obj.baz(); }"
        result = self.ext.extract("test.ts", source)
        calls = [e for e in result.edges if e.kind == "CALLS"]
        callee_names = {e.to_id for e in calls}
        assert "bar" in callee_names
        assert "baz" in callee_names

    def test_extracts_expects_returns(self):
        source = b"function process(order: Order): Receipt { return new Receipt(); }"
        result = self.ext.extract("test.ts", source)
        expects = [e for e in result.edges if "EXPECTS" in e.kind]
        returns = [e for e in result.edges if "RETURNS" in e.kind]
        assert any(e.to_id == "Order" for e in expects)
        assert any(e.to_id == "Receipt" for e in returns)

    def test_unwraps_promise_return_type(self):
        source = b"async function fetch(): Promise<Data> { return new Data(); }"
        result = self.ext.extract("test.ts", source)
        returns = [e for e in result.edges if "RETURNS" in e.kind]
        assert any(e.to_id == "Data" for e in returns)

    def test_method_visibility(self):
        source = b"class Foo { private bar() {} protected baz() {} qux() {} }"
        result = self.ext.extract("test.ts", source)
        funcs = {n.name: n.visibility for n in result.nodes if n.kind == "function"}
        assert funcs["bar"] == "private"
        assert funcs["baz"] == "protected"
        assert funcs["qux"] == "public"

    def test_extracts_this_method_calls(self):
        source = b"""
class Service {
    process(): void {}
    run(): void {
        const result = this.process();
    }
}
"""
        result = self.ext.extract("test.ts", source)
        calls = [e for e in result.edges if e.kind == "CALLS"]
        assert any(e.to_id == "process" and "run" in e.from_id for e in calls)


# ---------------------------------------------------------------------------
# JavaScript extractor
# ---------------------------------------------------------------------------

class TestJavaScriptExtractor:
    def setup_method(self):
        self.ext = JavaScriptExtractor()

    def test_extracts_functions(self):
        source = b"function hello(name) { return name; }"
        result = self.ext.extract("test.js", source)
        funcs = [n for n in result.nodes if n.kind == "function"]
        assert len(funcs) == 1
        assert funcs[0].name == "hello"

    def test_extracts_classes(self):
        source = b"class Foo extends Bar { constructor() { super(); } }"
        result = self.ext.extract("test.js", source)
        classes = [n for n in result.nodes if n.kind == "class"]
        assert len(classes) == 1
        inherits = [e for e in result.edges if e.kind == "INHERITS"]
        assert any(e.to_id == "Bar" for e in inherits)
