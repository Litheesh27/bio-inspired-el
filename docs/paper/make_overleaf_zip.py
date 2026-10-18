"""Stage an Overleaf-ready zip of the paper.

Overleaf has no access to paths outside the uploaded project, so the figure
PDFs -- which live in figures/ieee/ at the repo root -- are copied into
docs/paper/figures/ here. main.tex's \\graphicspath lists that directory
first, so the same sources build both locally and on Overleaf.

Usage:
    .venv/Scripts/python.exe docs/paper/make_overleaf_zip.py
"""
import os
import shutil
import sys
import zipfile

PAPER_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(PAPER_DIR, "..", ".."))
IEEE_FIGS = os.path.join(REPO_ROOT, "figures", "ieee")
STAGED_FIGS = os.path.join(PAPER_DIR, "figures")
OUT_ZIP = os.path.join(PAPER_DIR, "bioroute_paper_overleaf.zip")

# Only the figures the two documents actually \includegraphics. Add to this
# list if a new figure is referenced, rather than shipping all seven.
FIGURES = [
    "fig01_convergence.pdf",
    "fig02_recovery_by_scenario.pdf",
    "fig03_stretch_pre_post.pdf",
    "fig04_pareto_recovery_vs_quality.pdf",
    "fig07_warm_vs_cold.pdf",
]

ROOTS = ["main.tex", "main_conf.tex", "refs.bib", "README.md"]
TREES = ["sections", "conf"]


def main():
    if not os.path.isdir(IEEE_FIGS):
        sys.exit(
            f"IEEE figures not found at {IEEE_FIGS}\n"
            "Generate them first:\n"
            "  .venv/Scripts/python.exe scripts/04_make_figures.py --ieee\n"
            "  .venv/Scripts/python.exe scripts/06_make_traces.py --ieee"
        )

    os.makedirs(STAGED_FIGS, exist_ok=True)
    missing = []
    for fig in FIGURES:
        src = os.path.join(IEEE_FIGS, fig)
        if not os.path.isfile(src):
            missing.append(fig)
            continue
        shutil.copy2(src, os.path.join(STAGED_FIGS, fig))
    if missing:
        sys.exit("missing figure PDFs: " + ", ".join(missing))

    if os.path.exists(OUT_ZIP):
        os.remove(OUT_ZIP)

    n = 0
    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ROOTS:
            path = os.path.join(PAPER_DIR, name)
            if os.path.isfile(path):
                z.write(path, name)
                n += 1
        for tree in TREES:
            tree_dir = os.path.join(PAPER_DIR, tree)
            if not os.path.isdir(tree_dir):
                continue
            for fn in sorted(os.listdir(tree_dir)):
                if fn.endswith(".tex"):
                    z.write(os.path.join(tree_dir, fn), f"{tree}/{fn}")
                    n += 1
        for fig in FIGURES:
            z.write(os.path.join(STAGED_FIGS, fig), f"figures/{fig}")
            n += 1

    size_kb = os.path.getsize(OUT_ZIP) / 1024.0
    print(f"wrote {OUT_ZIP}")
    print(f"  {n} files, {size_kb:.1f} KB")
    print("Upload to Overleaf as a new project; set the main document to")
    print("main.tex (journal) or main_conf.tex (conference), compiler pdfLaTeX.")


if __name__ == "__main__":
    main()
