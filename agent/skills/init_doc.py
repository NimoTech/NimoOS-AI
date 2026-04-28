INIT_SYSTEM_PROMPT = """You are generating an `agent.md` file documenting a directory for future AI agent sessions. The directory is provided in the user message.

Workflow:
1. Use list_dir + glob_files to understand the structure (skip large data dirs).
2. Read 3-8 most informative files (README, package.json/pyproject.toml, main entrypoint, configs).
3. Use write_file to create `<target>/agent.md` with these sections:
   - **Purpose** (1 paragraph)
   - **Tech / stack** (bullets)
   - **Top-level layout** (tree, no more than 30 lines)
   - **Important files** (file -> why it matters)
   - **Conventions** (naming, build, test, deploy if obvious)
   - **Avoid** (any tricky pitfalls visible from the code)
4. Keep agent.md to 200 lines or fewer. End your turn with a one-paragraph summary in chat.

Do not invent structure. Do not modify other files. Only write_file the agent.md.
"""
