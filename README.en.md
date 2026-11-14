# Finagent MCP — financial reports for AI assistants

[Русский](https://github.com/finuchetai/finagent-mcp/blob/main/README.md) · [Download 0.7.0rc2](https://github.com/finuchetai/finagent-mcp/releases/tag/v0.7.0rc2) · [PyPI](https://pypi.org/project/open-finance-mcp/0.7.0rc2/) · [CI](https://github.com/finuchetai/finagent-mcp/actions/workflows/tests.yml) · [MIT license](https://github.com/finuchetai/finagent-mcp/blob/main/LICENSE)

<!-- mcp-name: io.github.finuchetai/finagent-mcp -->

Read prepared financial reports with your AI assistant through the Model Context Protocol (MCP). Ask about profit and loss, cash flow, and account balances. Finagent exposes report rows, methodology, available months, and notes; the assistant explains the data within those limits.

**0.7.0rc2 is a public prerelease.** The repository, release downloads, and Python package on PyPI are available. The Python distribution name is `open-finance-mcp`; the server module is `finance_mcp.server`. The official [MCP Registry](https://registry.modelcontextprotocol.io/) lists **`io.github.finuchetai/finagent-mcp`**, version `0.7.0rc2`, with active status. The Registry entry starts the synthetic demo; client sources require local setup.

## Sources and requirements

| Source | Setup | Google approval required? |
|---|---|---|
| Synthetic demo | Install the package; no credentials or financial files | No |
| Excel (.xlsx) and CSV | Map prepared monthly reports and reconcile them against the source | No |
| PostgreSQL | Approved views, a read-only role, a source map, and reconciliation | No |
| Google Sheets | Google OAuth and a supported monthly report layout | Google data-access verification is pending |

PostgreSQL support does not imply MySQL or Microsoft SQL Server support. This is an adapter for prepared reports, not an automatic accounting engine for arbitrary transaction lists.

Your agent can inspect an approved source, prepare the mapping, run checks, and help configure the MCP client. Start with [AGENTS.md](https://github.com/finuchetai/finagent-mcp/blob/main/AGENTS.md) and the [guided setup](https://github.com/finuchetai/finagent-mcp/blob/main/docs/guided-setup.md) (currently in Russian). The data owner confirms the meaning of the reports, monetary units, methodology, and whether a month is ready for analysis. The agent must reconcile all mapped rows; structural validation alone does not establish that amounts are correct.

## Install from PyPI

Create an isolated environment and install the exact prerelease:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'open-finance-mcp[files,postgres]==0.7.0rc2'
.venv/bin/python -m finance_mcp.diagnostics stdio --timeout 15
```

The source extras enable Excel/CSV and PostgreSQL; no Google credentials are required for these sources or the demo. On Windows, use `python` and `.venv\Scripts\python.exe`. Use the absolute interpreter path and `-m finance_mcp.server` in your compatible MCP client. Without a source configuration, reports contain synthetic demo data.

With `uv` installed, the Registry runtime can also be checked using `uvx --from open-finance-mcp==0.7.0rc2 open-finance-mcp --check`. Use a persistent venv and source-specific dependencies for client data.

## Run the demo from source

Requirements: Python 3.11+, venv/pip, and network access for the initial dependency installation. Run these commands in a separate checkout:

```sh
git clone https://github.com/finuchetai/finagent-mcp.git
cd finagent-mcp
python3 preflight.py environment
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m finance_mcp.diagnostics stdio --timeout 15
.venv/bin/python -m unittest discover -s tests -p test_demo.py -v
```

On Windows, use `python` if `python3` is unavailable and `.venv\Scripts\python.exe` in place of `.venv/bin/python`.

Configure a compatible local MCP client with the **absolute path** to that interpreter. The enclosing configuration format depends on the client:

```json
{
  "mcpServers": {
    "finagent-demo": {
      "command": "/absolute/path/to/finagent-mcp/.venv/bin/python",
      "args": ["-m", "finance_mcp.server"]
    }
  }
}
```

Without `--config`, the server uses synthetic demo data. It does not connect to your accounts. Ask the assistant to read the passport and available months before requesting reports.

Example: “Show September 2026 profit, cash movement, and closing balance. Explain why profit differs from cash movement.” Demo control totals are **75,000 RUB profit**, **80,000 RUB net cash movement**, and **155,000 RUB closing balance**. October 2026 is open and must be rejected. Demo dates are fixed for reproducibility.

## Connect Excel, CSV, or PostgreSQL

Install the source extras into the same venv:

```sh
.venv/bin/python -m pip install '.[files,postgres]'
.venv/bin/python -m finance_mcp.server --config examples/lipa/csv.json --check
```

The example uses a fictional business. For client data, prepare and reconcile a source configuration, then append `--config` and its absolute JSON path to the MCP server arguments. Do not use an administrator DSN; PostgreSQL credentials must be restricted to approved read-only views. Keep credentials outside the published package. See [source formats and permissions](https://github.com/finuchetai/finagent-mcp/blob/main/docs/sources.md).

The default transport is local stdio. A remote client requires the owner's HTTPS server and an OAuth provider; this repository implements the resource server, not hosted login or a shared financial endpoint. See [connection modes](https://github.com/finuchetai/finagent-mcp/blob/main/docs/connection-modes.md). Compatibility must be checked for the chosen client and account.

## Google Sheets on Windows with Codex

Download [finagent-google-codex-0.7.0rc2.zip](https://github.com/finuchetai/finagent-mcp/releases/download/v0.7.0rc2/finagent-google-codex-0.7.0rc2.zip), extract it into a separate folder, and run `Start.cmd`. You need installed Codex, Python 3.11+, and internet access. Follow the prompts for Google login, your existing spreadsheet link, report, month, currency, and monetary units. After successful setup, restart Codex and open a new chat to verify actual MCP tool calls.

As of **14 November 2026**, Google branding is published and data-access verification is pending. Broad Google sign-in is not advertised as approved. If Google refuses access or shows an unverified-app warning, stop and contact the author with the error text; bypassing the warning is not an installation step. Do not share passwords, authorization codes, or tokens.

The Windows bundle includes the author's Desktop OAuth application configuration. A Git clone, the source ZIP, and a plain Python install do not include it. User grants stay in the Windows credential store; the bundle contains no user grant or financial spreadsheet. Supported layouts and setup prompts are documented in [the Windows guide](https://github.com/finuchetai/finagent-mcp/blob/main/docs/google-local-connect.md), currently in Russian.

Cash-flow reading was verified by an external tester in Codex on Windows, including a new chat after restart. The final Windows bundle was checked with the owner's existing Google permission. A new Google login and a new Codex chat for that final build remain separate checks. End-user acceptance of balances and other clients/platforms is not claimed.

## Data boundaries

- Source data is read only. Installing the MCP does not authorize editing a workbook or database.
- Only approved closed months are accepted. A partial P&L is allowed with `complete=false`; missing expenses or taxes are not invented.
- Missing and not-applicable values remain distinct from zero. Totals and detail rows are not automatically added together.
- Reports identify currency, source, completeness, and timing. An unknown accounting date remains unknown; retrieval time does not replace it.
- The server holds a snapshot until restart. Google reports refresh automatically and fail on refresh errors. Stop the running process when revoking access.

## Validation and support

[CI for the published release](https://github.com/finuchetai/finagent-mcp/actions/runs/43607284202) passed stdio diagnostics and 98 tests on Python 3.11 and 3.13: 91 passed and 7 PostgreSQL runtime tests skipped on each version. Windows checks covered installation/reuse, 25 setup/demo/CSV/XLSX tests, and real Google/MCP reads with existing owner permission. Skipped tests are not PostgreSQL integration acceptance.

To disconnect, stop the MCP process and remove its client entry. For Codex use `codex mcp remove <server-name>`. Google permissions can also be revoked at [Google account connections](https://myaccount.google.com/connections).

Report bugs through [GitHub Issues](https://github.com/finuchetai/finagent-mcp/issues) without financial data, personal information, credentials, or tokens. See [SECURITY.md](https://github.com/finuchetai/finagent-mcp/blob/main/SECURITY.md), [CONTRIBUTING.md](https://github.com/finuchetai/finagent-mcp/blob/main/CONTRIBUTING.md), and [CHANGELOG.md](https://github.com/finuchetai/finagent-mcp/blob/main/CHANGELOG.md). These detailed documents are currently in Russian.

Self-hosting does not require a Finagent service account. For assistance with setup or management accounting, [contact the Finagent team](https://finuchetai.ru/?utm_source=github&utm_medium=opensource&utm_campaign=finance_mcp).
