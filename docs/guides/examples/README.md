# Example workbooks

- `veromesh-model-workbook-example.xlsx`: the worked example, supplier invoice approval (17 sheets, 29 rows). It loads with no errors, violations or warnings.
- `veromesh-model-workbook-blank.xlsx`: the empty template.

Both are what `veromesh-connect spreadsheet template` writes, and CI checks that they still are
(`connectors/tests/test_spreadsheet.py`). Any install writes its own, matched to its rules:

```bash
veromesh-connect --api https://<your install> spreadsheet template --example -o veromesh-model-workbook-example.xlsx
veromesh-connect --api https://<your install> spreadsheet template -o veromesh-model-workbook-blank.xlsx
```

How to fill one in and load it: the User Guide, "Maintaining the model in the workbook", and the
Workbook Reference for every column.
