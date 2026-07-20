"""
main.py
CodeScope MVP - command line version.

Usage:
    python main.py scan <folder_path>
        Parses supported source files in the folder and builds the dependency graph.

    python main.py impact <function_name>
        Reports what would be affected if you changed that function, with a risk score.

    python main.py map
        Shows the most important/central functions in the codebase.
"""

import sys
from parser import extract_facts_from_folder
from graph import CodeGraph
from risk import score_risk
from explain import explain_codebase
import textwrap
# ---- EDIT THESE to match your own Neo4j Desktop setup ----
import os
from dotenv import load_dotenv
load_dotenv()

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687").strip()
NEO4J_USERNAME = os.environ.get("NEO4J_USERNAME", "neo4j").strip()
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "changeme").strip()

def cmd_scan(folder_path):
    print(f"Scanning folder: {folder_path}")
    facts = extract_facts_from_folder(folder_path)
    print(f"Extracted {len(facts)} facts.")

    graph = CodeGraph(NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, "local-cli")   
    graph.insert_facts(facts)
    graph.close()
    print("Dependency graph updated in Neo4j.")


def cmd_impact(function_name):
    graph = CodeGraph(NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, "local-cli")
    affected = graph.find_impact(function_name)
    graph.close()

    report = score_risk(affected)

    print(f"\nImpact report for changing '{function_name}':")
    print(f"  Risk level: {report['risk_level']}")
    print(f"  Affected functions: {report['affected_count']}")
    for fn in report["affected_functions"]:
        print(f"    - {fn['name']}  (in {fn['file']})")


def cmd_map():
    graph = CodeGraph(NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, "local-cli")
    central = graph.find_central_functions(5)
    graph.close()

    print(f"\nMost important functions in this codebase (start here):")
    for fn in central:
        print(f"  - {fn['name']}  (in {fn['file']})  <- called from {fn['incoming_calls']} place(s)")

def cmd_explain():
    graph = CodeGraph(NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, "local-cli")
    central = graph.find_central_functions(5)
    edges = graph.get_all_edges()
    graph.close()

    explanation = explain_codebase(central, edges)

    print("=" * 60)
    print("CODEBASE OVERVIEW")
    print("=" * 60)
    wrapped = textwrap.fill(explanation, width=70)
    print(wrapped)
    print("=" * 60)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python main.py scan <folder_path>")
        print("  python main.py impact <function_name>")
        print("  python main.py map")
        print("  python main.py explain")
        sys.exit(1)

    command = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else None

    if command == "scan":
        cmd_scan(arg)
    elif command == "impact":
        cmd_impact(arg)
    elif command == "map":
        cmd_map()
    elif command == "explain":
        cmd_explain()
    else:
        print(f"Unknown command: {command}")