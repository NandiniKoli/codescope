"""
graph.py
Handles all Neo4j interactions: inserting parsed facts, and
running impact-analysis queries on the stored dependency graph.

Every node is tagged with a scan_id, so concurrent analyses (e.g. two
webhook events arriving close together) never wipe or read each other's
data. Each caller of CodeGraph must supply a unique scan_id per analysis
run (e.g. "owner/repo#pr_number" or a fresh uuid).
"""

from neo4j import GraphDatabase


class CodeGraph:
    def __init__(self, uri, username, password, scan_id):
        self.driver = GraphDatabase.driver(uri, auth=(username, password))
        self.scan_id = scan_id

    def close(self):
        self.driver.close()

    def clear_all(self):
        """Wipes ONLY this scan's data, not the whole database. Safe to call
        even while other scans are running concurrently."""
        with self.driver.session() as session:
            session.run(
                "MATCH (n:Function {scan_id: $scan_id}) DETACH DELETE n",
                scan_id=self.scan_id
            )

    def insert_facts(self, facts):
        function_facts = [f for f in facts if f["type"] == "FUNCTION_DEFINED"]
        call_facts = [f for f in facts if f["type"] == "CALLS"]
        import_facts = [f for f in facts if f["type"] == "IMPORTS"]

        file_imports = {}
        for f in import_facts:
            file_imports.setdefault(f["file"], set()).add(f["name"])

        with self.driver.session() as session:
            session.execute_write(self._insert_function_facts_tx, function_facts, self.scan_id)
            session.execute_write(self._insert_call_facts_tx, call_facts, file_imports, self.scan_id)

    def get_all_edges(self):
        with self.driver.session() as session:
            result = session.run("""
                MATCH (a:Function {scan_id: $scan_id})-[:CALLS]->(b:Function {scan_id: $scan_id})
                RETURN a.name AS caller, b.name AS callee
            """, scan_id=self.scan_id)
            return [{"caller": r["caller"], "callee": r["callee"]} for r in result]

    CROSS_FILE_FALLBACK_LANGUAGES = {"javascript", "typescript"}

    @staticmethod
    def _insert_function_facts_tx(tx, facts, scan_id):
        for fact in facts:
            tx.run(
                "MERGE (f:Function {name: $name, file: $file, scan_id: $scan_id}) SET f.lang = $lang",
                name=fact["name"], file=fact["file"], lang=fact["lang"], scan_id=scan_id
            )

    @staticmethod
    def _insert_call_facts_tx(tx, facts, file_imports, scan_id):
        for fact in facts:
            lang = fact["lang"]
            caller_file = fact["file"]
            callee = fact["callee"]

            allow_fallback = (
                lang in CodeGraph.CROSS_FILE_FALLBACK_LANGUAGES
                or (lang == "python" and callee in file_imports.get(caller_file, set()))
            )

            if allow_fallback:
                tx.run(
                    """
                    MATCH (a:Function {name: $caller, file: $file, scan_id: $scan_id})
                    OPTIONAL MATCH (b_same:Function {name: $callee, file: $file, scan_id: $scan_id})
                    OPTIONAL MATCH (b_other:Function {name: $callee, lang: $lang, scan_id: $scan_id})
                    WITH a, coalesce(b_same, b_other) AS b
                    WHERE b IS NOT NULL
                    MERGE (a)-[:CALLS]->(b)
                    """,
                    caller=fact["caller"], callee=callee, file=caller_file, lang=lang, scan_id=scan_id
                )
            else:
                tx.run(
                    """
                    MATCH (a:Function {name: $caller, file: $file, scan_id: $scan_id})
                    MATCH (b:Function {name: $callee, file: $file, scan_id: $scan_id})
                    MERGE (a)-[:CALLS]->(b)
                    """,
                    caller=fact["caller"], callee=callee, file=caller_file, scan_id=scan_id
                )

    def find_impact(self, function_name, max_hops=3):
        with self.driver.session() as session:
            return session.execute_read(self._find_impact_tx, function_name, max_hops, self.scan_id)

    @staticmethod
    def _find_impact_tx(tx, function_name, max_hops, scan_id):
        query = f"""
            MATCH (affected {{scan_id: $scan_id}})-[:CALLS*1..{max_hops}]->(target:Function {{name: $name, scan_id: $scan_id}})
            RETURN DISTINCT affected.name AS name, affected.file AS file
        """
        result = tx.run(query, name=function_name, scan_id=scan_id)
        return [{"name": r["name"], "file": r["file"]} for r in result]

    def find_central_functions(self, top_n=5):
        with self.driver.session() as session:
            return session.execute_read(self._find_central_tx, top_n, self.scan_id)

    @staticmethod
    def _find_central_tx(tx, top_n, scan_id):
        query = """
            MATCH (caller:Function {scan_id: $scan_id})-[:CALLS]->(target:Function {scan_id: $scan_id})
            WHERE target.file IS NOT NULL
            RETURN target.name AS name, target.file AS file, count(caller) AS incoming_calls
            ORDER BY incoming_calls DESC
            LIMIT $top_n
        """
        result = tx.run(query, top_n=top_n, scan_id=scan_id)
        return [{"name": r["name"], "file": r["file"], "incoming_calls": r["incoming_calls"]} for r in result]