import requests
import os

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

def explain_codebase(central_functions, edges):
    facts_text = "\n".join(
        f"{e['caller']} calls {e['callee']}" for e in edges
    )
    central_text = "\n".join(
        f"{f['name']} (in {f['file']}) - called from {f['incoming_calls']} places"
        for f in central_functions
    )

    prompt = f"""You are helping a student understand an unfamiliar codebase for the first time.

Here are function call relationships found in the code:
{facts_text}

Here are the most important (most depended-on) functions:
{central_text}

Write a short, friendly explanation (under 150 words) that tells a newcomer:
1. What this codebase seems to do overall
2. Which function to read first, and why
3. One function that looks risky to change carelessly, and why
Write in plain text only. Do not use markdown formatting like asterisks, backticks, or hash symbols."""
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
    except requests.exceptions.Timeout:
        return "Error: the explanation service took too long to respond (timed out)."
    except requests.exceptions.RequestException as e:
        return f"Error: could not reach the explanation service ({e})."

    data = response.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return f"Error from API: {data}"