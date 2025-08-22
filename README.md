# Community Notes Finder (Claude)

Streamlit app that:
1) Pulls posts eligible for Community Notes from X,
2) Uses Claude to flag **fact-checkable** posts,
3) Scores by **importance × checkability**,
4) Generates **starter research aids** (questions & keywords),
5) Exports a CSV for newsroom workflows.

## Quickstart

```bash
pip install -r requirements.txt
streamlit run app.py
