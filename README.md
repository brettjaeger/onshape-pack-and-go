# Onshape Pack and Go

Exports all released parts (STEP) and their linked drawings (PDF) from an Onshape assembly, organized into a folder and ZIP archive.

## What it does

1. Reads the assembly BOM and collects all **released** parts
2. Exports each part as a **STEP** file
3. Finds linked **drawings** whose part number and revision match the BOM
4. Exports each drawing as a **PDF**
5. Saves everything to `Exports/{assembly_name}/` and packages it as a ZIP

Files are named `PartNumber-Revision-PartName` (e.g. `RRP-504-A-Hard_Stop_Block.step` / `RRP-504-A-Hard_Stop_Block.pdf`).

## Requirements

- Python 3.8+
- An [Onshape API key](https://dev-portal.onshape.com/) (Access Key + Secret Key)

## Setup

1. Clone the repo:
   ```bash
   git clone https://github.com/brettjaeger/onshape-pack-and-go.git
   cd onshape-pack-and-go
   ```

2. Install dependencies:
   ```bash
   pip install requests python-dotenv
   ```

3. Create a `.env` file in the project root:
   ```
   ONSHAPE_ACCESS_KEY=your_access_key
   ONSHAPE_SECRET_KEY=your_secret_key
   ```

## Usage

### Command line

```bash
python pack_and_go.py "https://cad.onshape.com/documents/..."
```

Output is saved to `Exports/{assembly_name}/` with a ZIP at `Exports/{assembly_name}.zip`.

### Google Colab (Jupyter notebook)

1. Open `onshape_pack_and_go.ipynb` in [Google Colab](https://colab.research.google.com/)
2. Add `ONSHAPE_ACCESS_KEY` and `ONSHAPE_SECRET_KEY` to the Colab **Secrets** tab (🔑 in the left sidebar)
3. Paste your assembly URL in the config cell
4. Run all cells — the ZIP will download automatically

## Notes

- Only **released** parts (state = Released in the BOM) are exported
- Drawings are matched to BOM parts by **part number and revision** — a drawing released at a different revision than the BOM part will be skipped with a warning
- Parts with no matching drawing will show a warning but won't cause the export to fail

## License

MIT — see [LICENSE](LICENSE)
