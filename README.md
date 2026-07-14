# CodeScope

CodeScope analyzes a codebase's internal function dependencies and tells you what's safe to touch and what's risky, before you make a change — built for students, junior developers, and anyone new to a codebase.

## What it does

- Point it at a public GitHub repo (or a local folder)
- It parses the code and builds a dependency graph in Neo4j
- Ask it: **"what breaks if I change this function?"** — get a risk-scored answer (Low/Medium/High)
- Ask it: **"what are the most important functions to understand first?"** — get a starting map
- Ask it to **explain the codebase in plain English**, powered by an LLM

## Languages supported

Python, JavaScript, TypeScript, Java, C, C++, Go, Rust, Ruby, PHP, C#, Kotlin, Swift, Scala, Lua, Haskell, Bash

## Tech stack

- **Parsing:** Tree-sitter
- **Graph storage:** Neo4j
- **Backend:** Python, FastAPI
- **AI explanation layer:** Groq (Llama 3.3)
- **Packaging:** Docker

## Running locally

1. Set up a Python virtual environment and install dependencies:
```
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. Have a Neo4j instance running (Neo4j Desktop for local dev)

3. Set environment variables:
```
NEO4J_URI
NEO4J_USERNAME
NEO4J_PASSWORD
GROQ_API_KEY
```

4. Run the web server:
```
cd app
uvicorn server:app --reload --port 8000
```

5. Visit `http://127.0.0.1:8000/docs` to try it interactively

## Running with Docker

```
docker build -t codescope .
docker run -p 8000:8000 -e NEO4J_URI=... -e NEO4J_USERNAME=... -e NEO4J_PASSWORD=... -e GROQ_API_KEY=... codescope
```

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/scan` | POST | Clone and analyze a GitHub repo (`{"repo_url": "..."}`) |
| `/map` | GET | Most important/central functions in the codebase |
| `/impact/{function_name}` | GET | Risk report for changing a specific function |
| `/explain` | GET | Plain-English AI explanation of the codebase |

## Command-line usage (alternative to the web server)

```
python main.py scan <folder_or_path>
python main.py impact <function_name>
python main.py map
python main.py explain
```

## What's next

- PostgreSQL storage for historical scan/impact reports
- GitHub webhook / Action integration for on-demand PR comments
- Deployment to a cloud host with Neo4j Aura
- Web dashboard