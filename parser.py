import os
from tree_sitter import Language, Parser

import tree_sitter_python as tspython
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
import tree_sitter_java as tsjava
import tree_sitter_c as tsc
import tree_sitter_cpp as tscpp
import tree_sitter_go as tsgo
import tree_sitter_rust as tsrust
import tree_sitter_ruby as tsruby
import tree_sitter_php as tsphp
import tree_sitter_c_sharp as tscsharp
import tree_sitter_kotlin as tskotlin
import tree_sitter_swift as tsswift
import tree_sitter_scala as tsscala
import tree_sitter_lua as tslua
import tree_sitter_haskell as tshaskell
import tree_sitter_bash as tsbash

# ---- Language registry ----
LANGUAGES = {
    "python": {
        "extensions": [".py"],
        "language": Language(tspython.language()),
        "def_types": ["function_definition"],
        "call_type": "call",
        "call_func_field": "function",
    },
    "javascript": {
        "extensions": [".js", ".jsx", ".mjs"],
        "language": Language(tsjs.language()),
        "def_types": ["function_declaration", "method_definition"],
        "call_type": "call_expression",
        "call_func_field": "function",
    },
    "typescript": {
        "extensions": [".ts", ".tsx"],
        "language": Language(tsts.language_typescript()),
        "def_types": ["function_declaration", "method_definition"],
        "call_type": "call_expression",
        "call_func_field": "function",
    },
    "java": {
        "extensions": [".java"],
        "language": Language(tsjava.language()),
        "def_types": ["method_declaration", "constructor_declaration"],
        "call_type": "method_invocation",
        "call_func_field": "name",
    },
    "c": {
        "extensions": [".c", ".h"],
        "language": Language(tsc.language()),
        "def_types": ["function_definition"],
        "call_type": "call_expression",
        "call_func_field": "function",
    },
    "cpp": {
        "extensions": [".cpp", ".cc", ".hpp", ".cxx"],
        "language": Language(tscpp.language()),
        "def_types": ["function_definition"],
        "call_type": "call_expression",
        "call_func_field": "function",
    },
    "go": {
        "extensions": [".go"],
        "language": Language(tsgo.language()),
        "def_types": ["function_declaration", "method_declaration"],
        "call_type": "call_expression",
        "call_func_field": "function",
    },
    "rust": {
        "extensions": [".rs"],
        "language": Language(tsrust.language()),
        "def_types": ["function_item"],
        "call_type": "call_expression",
        "call_func_field": "function",
    },
    "ruby": {
        "extensions": [".rb"],
        "language": Language(tsruby.language()),
        "def_types": ["method", "singleton_method"],
        "call_type": "call",
        "call_func_field": "method",
    },
    "php": {
        "extensions": [".php"],
        "language": Language(tsphp.language_php()),
        "def_types": ["function_definition"],
        "call_type": "function_call_expression",
        "call_func_field": "function",
    },
    "csharp": {
        "extensions": [".cs"],
        "language": Language(tscsharp.language()),
        "def_types": ["method_declaration"],
        "call_type": "invocation_expression",
        "call_func_field": "function",
    },
    "kotlin": {
        "extensions": [".kt", ".kts"],
        "language": Language(tskotlin.language()),
        "def_types": ["function_declaration"],
        "call_type": "call_expression",
        "call_func_field": None,
    },
    "swift": {
        "extensions": [".swift"],
        "language": Language(tsswift.language()),
        "def_types": ["function_declaration"],
        "call_type": "call_expression",
        "call_func_field": None,
    },
    "scala": {
        "extensions": [".scala"],
        "language": Language(tsscala.language()),
        "def_types": ["function_definition"],
        "call_type": "call_expression",
        "call_func_field": "function",
    },
    # NOTE: Dart intentionally left out. Its grammar puts function_signature
    # and function_body as SIBLINGS (not parent/child), so the current_function
    # context set at function_signature never reaches calls inside function_body.
    # Calls also aren't a "method_invocation" node at all in this grammar --
    # they're identifier + selector -> argument_part -> arguments, a shape this
    # extractor doesn't support yet. Needs dedicated handling, not a config tweak.
    "lua": {
        "extensions": [".lua"],
        "language": Language(tslua.language()),
        "def_types": ["function_declaration", "function_definition"],
        "call_type": "function_call",
        "call_func_field": "name",
    },
    "haskell": {
        "extensions": [".hs"],
        "language": Language(tshaskell.language()),
        "def_types": ["function"],
        "call_type": "apply",
        "call_func_field": "function",
    },
    "bash": {
        "extensions": [".sh", ".bash"],
        "language": Language(tsbash.language()),
        "def_types": ["function_definition"],
        "call_type": "command",
        "call_func_field": "name",
    },
}

def _try_register(name, extensions, module_name, lang_attr, def_types, call_type, call_func_field):
    try:
        mod = __import__(module_name)
        lang_obj = getattr(mod, lang_attr)()
        LANGUAGES[name] = {
            "extensions": extensions,
            "language": Language(lang_obj),
            "def_types": def_types,
            "call_type": call_type,
            "call_func_field": call_func_field,
        }
    except ImportError:
        print(f"[parser] Skipping {name}: {module_name} not installed")

# Common built-in/standard-library method names that create noise in the graph.
# These are generic enough to appear in almost any codebase without being
# meaningful, project-specific relationships.
COMMON_BUILTIN_CALLS = {
    # Python dict/list/object built-ins
    "get", "set", "setdefault", "pop", "update", "append", "extend",
    "items", "keys", "values", "join", "split", "format", "strip",
    "replace", "sort", "sorted", "len", "str", "int", "float", "list",
    "dict", "range", "print", "isinstance", "super", "open", "close",
    # JS/general built-ins
    "call", "apply", "bind", "push", "pop", "map", "filter", "reduce",
    "forEach", "toString", "valueOf", "hasOwnProperty",
    # Common across many languages
    "toString", "equals", "hashCode", "clone", "next", "hasNext",
}

EXT_TO_LANG = {}
for lang_name, cfg in LANGUAGES.items():
    for ext in cfg["extensions"]:
        EXT_TO_LANG[ext] = lang_name


def get_function_name(node, code, lang_cfg):
    name_node = node.child_by_field_name("name")
    if name_node:
        return code[name_node.start_byte:name_node.end_byte].decode("utf8", errors="ignore")
    declarator = node.child_by_field_name("declarator")
    if declarator:
        inner = declarator.child_by_field_name("declarator")
        target = inner if inner else declarator
        return code[target.start_byte:target.end_byte].decode("utf8", errors="ignore")
    return None


def extract_facts_from_file(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    lang_name = EXT_TO_LANG.get(ext)
    if not lang_name:
        return []

    lang_cfg = LANGUAGES[lang_name]
    parser = Parser(lang_cfg["language"])

    with open(filepath, "rb") as f:
        code = f.read()

    tree = parser.parse(code)
    filename = os.path.basename(filepath)
    facts = []

    def walk(node, current_function=None):
        if node.type in lang_cfg["def_types"]:
            fname = get_function_name(node, code, lang_cfg)
            if fname:
                current_function = fname
                facts.append({
                    "type": "FUNCTION_DEFINED",
                    "name": current_function,
                    "file": filename,
                    "lang": lang_name
                })        
        if node.type == lang_cfg["call_type"]:
            call_field = lang_cfg["call_func_field"]
            if call_field:
                func_node = node.child_by_field_name(call_field)
            else:
                func_node = node.children[0] if node.children else None
            if func_node and current_function and func_node.type != lang_cfg["call_type"]:
                called_name = code[func_node.start_byte:func_node.end_byte].decode("utf8", errors="ignore")
                called_name = called_name.split(".")[-1].split("::")[-1]
                if called_name not in COMMON_BUILTIN_CALLS:
                    facts.append({
                        "type": "CALLS",
                        "caller": current_function,
                        "callee": called_name,
                        "file": filename,
                        "lang": lang_name
                    })

        for child in node.children:
            walk(child, current_function)

    walk(tree.root_node)
    return facts


def extract_facts_from_folder(folder_path):
    EXCLUDE_DIRS = {"test", "tests", "node_modules", "docs", "test-treeshake",
                     ".git", ".github", "build", "dist", "__pycache__", "venv", ".venv"}
    EXCLUDE_PATTERNS = ["-min", "-umd", "-esm", "-node"]
    EXCLUDE_FILENAMES = {"underscore.js"}  # bundled build output, not real source

    all_facts = []
    for root, dirs, files in os.walk(folder_path):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if (ext in EXT_TO_LANG
                    and fname not in EXCLUDE_FILENAMES
                    and not any(p in fname for p in EXCLUDE_PATTERNS)):
                all_facts.extend(extract_facts_from_file(os.path.join(root, fname)))
    return all_facts