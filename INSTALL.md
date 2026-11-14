# Installing ShodhKosha on Windows

This guide takes a Windows 10 or 11 PC from nothing to a working ShodhKosha.
Allow about 20 minutes, most of it downloads.

## 1. What you need

| Program | Needed for | Required? |
|---|---|---|
| Python 3.12 or newer | Running ShodhKosha | Yes |
| Git | Downloading the code | Yes |
| Ollama | AI summaries, tags, filing, study cards | Recommended |
| Tesseract OCR | Searching scanned PDFs | Optional |

Open **PowerShell** and install them:

```powershell
winget install Python.Python.3.12
winget install Git.Git
winget install Ollama.Ollama
winget install UB-Mannheim.TesseractOCR
```

Close PowerShell and open it again so the new programs are found.

## 2. Get the code

```powershell
cd $HOME\Documents
git clone https://github.com/magnanijeevesh/ShodhKosha.git ShodhKosha
cd ShodhKosha
```

## 3. Install the Python libraries

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The prompt now starts with `(.venv)`. If PowerShell refuses to run
`Activate.ps1`, run this once and try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 4. Download the AI model

```powershell
ollama pull llama3.2
```

This downloads about 2 GB once. For better folders and tags on a PC with a
graphics card, also run `ollama pull qwen2.5:7b` and pick it in ShodhKosha
under **Tools > Choose AI Model**.

## 5. Check the installation

```powershell
python -m unittest discover tests
```

The last line should be `OK`.

## 6. Start ShodhKosha

```powershell
python -m shodhkosha
```

Each later time, open PowerShell and run:

```powershell
cd $HOME\Documents\ShodhKosha
.venv\Scripts\Activate.ps1
python -m shodhkosha
```

## 7. Optional: build a one-click program

```powershell
pip install pyinstaller
pyinstaller --noconfirm --windowed --name ShodhKosha run_shodhkosha.py
```

The program is `dist\ShodhKosha\ShodhKosha.exe`. Right-click it and choose
**Send to > Desktop (create shortcut)**. Keep the whole `dist\ShodhKosha`
folder together; the .exe needs the files beside it. Ollama and Tesseract
stay separate programs.

## Where your library is kept

```
C:\Users\<you>\ShodhKosha Library\
    library.sqlite3     the database
    papers\             the library's copy of every imported PDF
```

Copy that folder to back up everything. It is separate from the code, so
updating or reinstalling ShodhKosha does not touch it.

## Updating

```powershell
cd $HOME\Documents\ShodhKosha
git pull
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If you use the .exe, build it again after updating.

## What uses the internet

Only two things, and both can be avoided:

- **DOI lookup** sends a paper's DOI to Crossref to fetch its details. Turn
  the automatic lookup off under **Tools > Look Up DOIs Automatically**.
- **Installing** the programs and the AI model in the steps above.

Reading, searching, annotating, OCR and all AI features run on your PC.

## Troubleshooting

| What you see | What to do |
|---|---|
| `python` is not recognized | Close and reopen PowerShell. If it persists, reinstall Python and tick "Add python.exe to PATH". |
| `No module named 'shodhkosha'` | Run the command from inside the `ShodhKosha` folder, the one that contains `shodhkosha` and `tests`. |
| `No module named 'PyQt6'` (or `pymupdf`) | The virtual environment is not active. Run `.venv\Scripts\Activate.ps1`, then `pip install -r requirements.txt`. |
| Status bar says "AI unavailable" | Start Ollama from the Start menu, then right-click a reference and choose **Re-analyze with AI**. |
| "The AI model ... is not installed" | Run `ollama pull llama3.2`. |
| Scanned PDFs cannot be searched | Install Tesseract (step 1) and restart ShodhKosha. |
| Read Aloud is greyed out | Your PyQt6 build lacks the speech component; the rest of the app is unaffected. |
| Save & Replace PDF fails | The PDF is open in another program such as Acrobat. Close it there and save again. |
| The .exe does not open | Run `dist\ShodhKosha\ShodhKosha.exe` from PowerShell to see the error message. |
