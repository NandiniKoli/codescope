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
        with self.driver.session() as session:
            session.execute_write(self._insert_facts_tx, function_facts)
            session.execute_write(self._insert_facts_tx, call_facts)

    def get_all_edges(self):
        with self.driver.session() as session:
            result = session.run("""
                MATCH (a:Function)-[:CALLS]->(b:Function)
                RETURN a.name AS caller, b.name AS callee
            """)
            return [{"caller": r["caller"], "callee": r["callee"]} for r in result]

    @staticmethod
    def _insert_facts_tx(tx, facts):
        for fact in facts:
            if fact["type"] == "FUNCTION_DEFINED":
                tx.run(
                    "MERGE (f:Function {name: $name, file: $file}) SET f.lang = $lang",
                    name=fact["name"], file=fact["file"], lang=fact["lang"]
                )
            elif fact["type"] == "CALLS":
                tx.run(
                    """
                    MATCH (a:Function {name: $caller, lang: $lang})
                    MATCH (b:Function {name: $callee, lang: $lang})
                    MERGE (a)-[:CALLS]->(b)
                    """,
                    caller=fact["caller"], callee=fact["callee"], lang=fact["lang"]
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