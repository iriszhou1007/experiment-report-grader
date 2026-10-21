# Signature template (per machine, never packaged)

The approved write-back copies the teacher's signature ink from a template PDF onto the
「指导老师评议」 page. That ink belongs to a person, not to a skill, so this directory exists only
as a convenient place to keep it on a **single-machine** setup — it is never committed
(`.gitignore` excludes `*.pdf` here) and `package_skill.py` excludes it from every archive.

Resolution order for the template (first hit wins):

1. `--signature-pdf /path/to/template.pdf`
2. `EXPERIMENT_REPORT_GRADER_SIGNATURE_PDF=/path/to/template.pdf`
3. `"signature_pdf"` in `config/local.json` — what `bootstrap.py --write-config` records
4. a PDF in this directory (`assets/signature/*.pdf`, ideally `signature.pdf`)

There is deliberately no fifth layer. When none of these resolve, `doctor` and `bootstrap.py`
report the signature as a blocking per-machine item, and `insert_feedback_page.py` refuses to
write rather than silently skipping the signature.

To check the current state:

```bash
python3 scripts/bootstrap.py            # per-machine assets, with the layer that answered
python3 scripts/batch_pipeline.py doctor
```

The template is a PDF carrying the signature as a PDF Ink annotation next to the 指导教师签名
label; `scripts/insert_feedback_page.py` copies that annotation. See
[../../references/pdf-writeback.md](../../references/pdf-writeback.md) for the full write-back
behavior.
