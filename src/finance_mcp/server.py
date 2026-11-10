"""Create six read-only tools for any FinanceAdapter; CLI serves synthetic demo over stdio."""
from dataclasses import asdict
import argparse
from decimal import Decimal
import json

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from .contract import CONTRACT_VERSION, ContractError, FinanceAdapter, FinanceService
from .demo import DemoAdapter


def encode(value) -> str:
    # Decimal is a JSON string deliberately: no binary floating-point rounding.
    return json.dumps(value, ensure_ascii=False,
                      default=lambda v: str(v) if isinstance(v, Decimal) else asdict(v))


def create_server(adapter: FinanceAdapter) -> MCPServer:
    service = FinanceService(adapter)
    server = MCPServer(name="open-finance", version="0.3.0",
                       instructions=adapter.passport())
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                         idempotentHint=True, openWorldHint=False)

    def checked(fn, *args):
        try:
            return fn(*args)
        except ContractError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=ro, structured_output=False)
    def get_passport() -> str:
        """Read the methodology, data origin, completeness and demo limitations first."""
        return adapter.passport()

    @server.tool(annotations=ro, structured_output=False)
    def list_months() -> str:
        """List periods, closed/open state and available reports. Open periods cannot be queried."""
        return encode({"contract_version": CONTRACT_VERSION, "periods": checked(service.periods)})

    def output(kind, months):
        return encode({"contract_version": CONTRACT_VERSION, "reports": checked(service.reports, kind, months)})

    @server.tool(annotations=ro, structured_output=False)
    def get_pnl(months: list[str]) -> str:
        """Accrual P&L for closed periods. as_of=null means unknown data date; received_at is retrieval time, not freshness."""
        return output("pnl", months)

    @server.tool(annotations=ro, structured_output=False)
    def get_cashflow(months: list[str]) -> str:
        """Cash movements, not profit. Unknown as_of cannot be replaced by received_at or used to claim fresh data."""
        return output("cashflow", months)

    @server.tool(annotations=ro, structured_output=False)
    def get_balances(months: list[str]) -> str:
        """Opening/movement/closing balances. as_of is the data-state date; received_at is only retrieval time."""
        return output("balances", months)

    @server.tool(annotations=ro, structured_output=False)
    def get_month_notes(month: str) -> str:
        """Methodological notes for a single closed YYYY-MM period."""
        return checked(service.notes, month)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only finance MCP (stdio)")
    parser.add_argument("--config", help="JSON source configuration; defaults to synthetic demo")
    parser.add_argument("--check", action="store_true", help="Validate snapshot and print counts, then exit")
    args = parser.parse_args()
    try:
        if args.config:
            from .sources import load_adapter
            adapter = load_adapter(args.config)
        else:
            adapter = DemoAdapter()
        if args.check:
            service = FinanceService(adapter)
            periods = service.periods()
            reports = [r for p in periods if p.closed for kind in p.reports
                       for r in service.reports(kind, [p.month])]
            result = {"contract_version": CONTRACT_VERSION, "periods": len(periods),
                      "reports": len(reports), "rows": sum(len(r.rows) for r in reports)}
            unknown = [f"{r.month}/{r.kind}" for r in reports if r.as_of_status == "unknown"]
            if unknown:
                result["warnings"] = ["Неизвестна дата состояния учёта: " + ", ".join(unknown)]
            print(encode(result))
        else:
            create_server(adapter).run(transport="stdio")
    except ContractError as exc:
        parser.exit(2, f"Ошибка источника: {exc}\n")


if __name__ == "__main__":
    main()
