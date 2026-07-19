"""
run_real_world_tests.py
Runs CodeScope's full pipeline against a battery of real, diverse repos,
and surfaces anything that LOOKS structurally suspicious for manual review
-- not "correct vs incorrect" (we don't know the right answer ahead of time
for real repos), but red flags worth a human double-checking.
"""

import os
import subprocess
import sys
import tempfile
import shutil
from dotenv import load_dotenv
from parser import extract_facts_from_folder
from graph import CodeGraph

load_dotenv()

NEO4J_URI = os.environ.get("NEO4J_URI")
NEO4J_USERNAME = os.environ.get("NEO4J_USERNAME")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD")

if not all([NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD]):
    sys.exit(
        "Missing NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD. "
        "Set them in your .env file (see .env.example) before running this script."
    )

REPOS = [
    # Already validated, keep as regression checks
    "https://github.com/jashkenas/underscore.git",
    "https://github.com/pallets/flask.git",
    "https://github.com/pallets/click.git",
    "https://github.com/psf/requests.git",
    "https://github.com/expressjs/express.git",
    # New languages, not yet stress-tested on real code
    "https://github.com/gin-gonic/gin.git",          # Go
    "https://github.com/BurntSushi/ripgrep.git",     # Rust
    "https://github.com/google/gson.git",            # Java
    "https://github.com/nlohmann/json.git",          # C++
    "https://github.com/JamesNK/Newtonsoft.Json.git", # C#
]


def analyze_repo(repo_url):
    temp_dir = tempfile.mkdtemp()
    red_flags = []
    try:
        subprocess.run(["git", "clone", "--depth", "1", repo_url, temp_dir],
                        check=True, capture_output=True, timeout=90)
        facts = extract_facts_from_folder(temp_dir)

        graph = CodeGraph(NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD)
        graph.clear_all()
        graph.insert_facts(facts)
        edges = graph.get_all_edges()
        central = graph.find_central_functions(10)
        graph.close()

        # Self-loops are informational only -- many are legitimate
        # (a class method calling another method of the same object via
        # self., or genuine recursion). Only worth a manual look if there
        # are an unusually large number relative to repo size, which could
        # still indicate an unresolved parser edge case.
        self_loops = [e for e in edges if e["caller"] == e["callee"]]
        if len(self_loops) > 30:
            red_flags.append(f"Unusually high self-loop count ({len(self_loops)}) -- may warrant investigation")

        # Red flag 2: a single function with an implausibly huge number of
        # incoming calls relative to repo size -- possible sign of a
        # name-collision bug fanning out incorrectly, like the clean_text bug
        if central and central[0]["incoming_calls"] > 100:
            red_flags.append(f"Suspiciously high fan-in: {central[0]['name']} = {central[0]['incoming_calls']}")

        return {
            "repo": repo_url,
            "facts_count": len(facts),
            "top_functions": central[:5],
            "red_flags": red_flags,
        }
    except Exception as e:
        return {"repo": repo_url, "error": str(e)}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    for repo in REPOS:
        print(f"\n{'='*60}\nTesting: {repo}\n{'='*60}")
        result = analyze_repo(repo)
        if "error" in result:
            print(f"ERROR: {result['error']}")
            continue
        print(f"Facts extracted: {result['facts_count']}")
        print("Top functions:")
        for fn in result["top_functions"]:
            print(f"  - {fn['name']} ({fn['file']}) <- {fn['incoming_calls']} calls")
        if result["red_flags"]:
            print("RED FLAGS (needs manual review):")
            for flag in result["red_flags"]:
                print(f"  \u26a0 {flag}")
        else:
            print("No automatic red flags.")