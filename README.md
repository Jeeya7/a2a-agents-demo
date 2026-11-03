# AI Club: Study Planner Agent

A small terminal demo using Gemini with the OpenAI Agents SDK: one model, one local tool,
and tool calls printed on screen. This is a single-agent demo, not an A2A server.

## Run

Open PowerShell in this folder. Your installed Python already has the required
packages. If you use a different environment, install them first:

```powershell
python -m pip install -r requirements.txt
```

Create a Gemini key at https://aistudio.google.com/apikey.
Open `.env`, enter it after `GEMINI_API_KEY=`, and save it.
The default model is `gemini-3.1-flash-lite`, which has a limited free API tier.
Use an eligible project on the free tier to avoid paid usage. Quotas and model
availability depend on your account. You can set another model with `GEMINI_MODEL`.
Existing OpenAI settings in `.env` are ignored by this demo.

The Agents SDK still manages the tool loop. Its model client sends requests to
Google's OpenAI-compatible endpoint using Chat Completions. No OpenAI key is needed.

```powershell
python main.py --demo
```

For a conversation with follow-up requests:

```powershell
python main.py
```

Commands: `/demo`, `/reset`, `/quit`.

## Two-minute presentation

1. Show `topics.json`: these are fictional, prepared ratings.
2. Show the `@function_tool` function in `main.py`: ordinary Python reads the file.
3. Run `python main.py --demo`. Watch the actual tool input and result appear.
4. Explain that the model requests the tool, Python executes it, and the model
   uses its returned data to write the plan. A successful run should show a
   calculus lookup and a three-day plan fitting 90 minutes per evening.
5. In interactive mode, ask: "Now I only have 45 minutes each evening. Revise it."
6. For a fresh demonstration, change integrals from `hard` to `easy` in the JSON
   and rerun the demo. The plan can vary, but it should reflect the new rating.

## Local check (no API call)

```powershell
python main.py --check
```

This verifies imports, the agent and tool definition, and topic data. It does
not verify API credentials, model access, or the quality of a generated plan.

To exercise the full SDK loop with simulated model responses and the real local
tool (no key or external API request):

```powershell
python check_demo_offline.py
```

The `.env` is ignored by Git. Tracing is disabled for this simple demo.
Conversation history stays in memory and clears when the program exits.

## Learn more

- [Gemini API key](https://aistudio.google.com/apikey)
- [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
- [Gemini free-tier pricing](https://ai.google.dev/gemini-api/docs/pricing)
