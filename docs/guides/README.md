# Guides

| Guide | For |
| --- | --- |
| [Administrator Guide](admin-guide.md) | Installing, signing in, connecting sources, running and recovering VeroMesh |
| [User Guide](user-guide.md) | Owners, stewards, model editors and agent builders |

The Markdown files are the source. CI builds a Word copy of each (the `guides` artifact of the `guides` job); to build them locally:

```bash
pip install python-docx   # and pandoc 3.x
python scripts/guides/build_docx.py      # writes .build/guides/*.docx
```

Sections marked "To write" are still skeleton.
