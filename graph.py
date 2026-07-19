"""
graph.py
Handles all Neo4j interactions: inserting parsed facts, and
running impact-analysis queries on the stored dependency graph.
"""

from neo4j import GraphDatabase


class CodeGraph:
    def __init__(self, uri, username, password):
        self.driver = GraphDatabase.driver(uri, auth=(username, password))

    def close(self):
        self.driver.close()

    def clear_all(self):
        """Wipes the graph. Useful while testing, NOT for production use."""
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")

    def insert_facts(self, facts):
        function_facts = [f for f in facts if f["type"] == "FUNCTION_DEFINED"]
        call_facts = [f for f in facts if f["type"] == "CALLS"]
        import_facts = [f for f in facts if f["type"] == "IMPORTS"]

        # Map: file -> set of names that file explicitly imported.
        # Used to verify a cross-file call is real, not guessed.
        file_imports = {}
        for f in import_facts:
            file_imports.setdefault(f["file"], set()).add(f["name"])

        with self.driver.session() as session:
            session.execute_write(self._insert_function_facts_tx, function_facts)
            session.execute_write(self._insert_call_facts_tx, call_facts, file_imports)

    def get_all_edges(self):
        with self.driver.session() as session:
            result = session.run("""
                MATCH (a:Function)-[:CALLS]->(b:Function)
                RETURN a.name AS caller, b.name AS callee
            """)
            return [{"caller": r["caller"], "callee": r["callee"]} for r in result]

    # Languages where a same-named function is genuinely likely to be
    # defined in one file and called from another (common module-splitting
    # convention, confirmed with underscore.js). Cross-file fallback matching
    # is safe here without needing an explicit import check. All other
    # languages default to strict same-file matching -- EXCEPT Python, which
    # now uses verified import-based matching instead (see _insert_call_facts_tx).
    # Heavy reuse of common method/function names across unrelated types
    # (Go's String()/New(), Rust's new()/as_bytes(), Java's get(), C++'s
    # size()/create()) makes ungated project-wide name matching produce
    # massive false fan-in -- confirmed across gin, ripgrep, gson, and
    # nlohmann/json in real-world testing.
    CROSS_FILE_FALLBACK_LANGUAGES = {"javascript", "typescript"}

    @staticmethod
    def _insert_function_facts_tx(tx, facts):
        for fact in facts:
            tx.run(
                "MERGE (f:Function {name: $name, file: $file}) SET f.lang = $lang",
                name=fact["name"], file=fact["file"], lang=fact["lang"]
            )

    @staticmethod
    def _insert_call_facts_tx(tx, facts, file_imports):
        for fact in facts:
            lang = fact["lang"]
            caller_file = fact["file"]
            callee = fact["callee"]

            # JS/TS: project-wide fallback confirmed safe/needed (underscore.js).
            # Python: fallback only if this file explicitly imported that exact
            # name -- a verified fact from the source, not a guess.
            allow_fallback = (
                lang in CodeGraph.CROSS_FILE_FALLBACK_LANGUAGES
                or (lang == "python" and callee in file_imports.get(caller_file, set()))
            )

            if allow_fallback:
                tx.run(
                    """
                    MATCH (a:Function {name: $caller, file: $file})
                    OPTIONAL MATCH (b_same:Function {name: $callee, file: $file})
                    OPTIONAL MATCH (b_other:Function {name: $callee, lang: $lang})
                    WITH a, coalesce(b_same, b_other) AS b
                    WHERE b IS NOT NULL
                    MERGE (a)-[:CALLS]->(b)
                    """,
                    caller=fact["caller"], callee=callee, file=caller_file, lang=lang
                )
            else:
                tx.run(
                    """
                    MATCH (a:Function {name: $caller, file: $file})
                    MATCH (b:Function {name: $callee, file: $file})
                    MERGE (a)-[:CALLS]->(b)
                    """,
                    caller=fact["caller"], callee=callee, file=caller_file
                )

    def find_impact(self, function_name, max_hops=3):
        """Returns every function that depends (directly or indirectly) on function_name."""
        with self.driver.session() as session:
            result = session.execute_read(self._find_impact_tx, function_name, max_hops)
            return result

    @staticmethod
    def _find_impact_tx(tx, function_name, max_hops):
        query = f"""
            MATCH (affected)-[:CALLS*1..{max_hops}]->(target:Function {{name: $name}})
            RETURN DISTINCT affected.name AS name, affected.file AS file
        """
        result = tx.run(query, name=function_name)
        return [{"name": r["name"], "file": r["file"]} for r in result]

    def find_central_functions(self, top_n=5):
        with self.driver.session() as session:
            return session.execute_read(self._find_central_tx, top_n)

    @staticmethod
    def _find_central_tx(tx, top_n):
        query = """
            MATCH (caller:Function)-[:CALLS]->(target:Function)
            WHERE target.file IS NOT NULL
            RETURN target.name AS name, target.file AS file, count(caller) AS incoming_calls
            ORDER BY incoming_calls DESC
            LIMIT $top_n
        """
        result = tx.run(query, top_n=top_n)
        return [{"name": r["name"], "file": r["file"], "incoming_calls": r["incoming_calls"]} for r in result]