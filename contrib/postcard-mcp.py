#!/usr/bin/python3
"""MCP de Postcard (stdio, JSON-RPC por líneas, sin dependencias externas).

Lee la base de Postcard (~/.local/share/postcard/postcard.db, de solo lectura) para listar cuentas, carpetas y
cabeceras al instante, y baja el cuerpo completo de un mensaje por IMAP usando las credenciales que Postcard guarda en
el llavero (reutiliza postcard.core.secrets.credential_for: contraseña para iCloud, XOAUTH2 para Gmail). Sustituye al
MCP de Thunderbird. Debe lanzarse con el entorno de /opt/gtk420 (lo pone el wrapper postcard-mcp).
"""
import sys, os, json, sqlite3, imaplib, ssl, email, email.policy, re
sys.path.insert(0, "/opt/gtk420/lib/python3/dist-packages")
from postcard.core.store.database import Database
from postcard.core import secrets as pc_secrets
from postcard.core.net.auth import MECHANISM_XOAUTH2, xoauth2_response

DB_PATH = os.path.expanduser("~/.local/share/postcard/postcard.db")


def _db():
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _accounts():
    return {a.id: a for a in Database().accounts()}


# ---- herramientas ----
def tool_listAccounts(_args):
    c = _db(); out = []
    for r in c.execute("""SELECT a.id, a.email, a.display_name,
            COALESCE(SUM(CASE WHEN f.name='INBOX' THEN e.unread ELSE 0 END),0) inbox_unread,
            COUNT(e.id) downloaded
            FROM accounts a LEFT JOIN folders f ON f.account_id=a.id
            LEFT JOIN emails e ON e.folder_id=f.id GROUP BY a.id ORDER BY a.id"""):
        out.append(dict(id=r["id"], email=r["email"], name=r["display_name"],
                        inbox_unread=r["inbox_unread"], downloaded=r["downloaded"]))
    c.close(); return out


def tool_listFolders(args):
    c = _db(); q = "SELECT f.id, f.account_id, f.name, COUNT(e.id) n, COALESCE(SUM(e.unread),0) unread FROM folders f LEFT JOIN emails e ON e.folder_id=f.id"
    params = []
    if args.get("account_id"): q += " WHERE f.account_id=?"; params.append(args["account_id"])
    q += " GROUP BY f.id ORDER BY f.account_id, f.name"
    out = [dict(id=r["id"], account_id=r["account_id"], name=r["name"], count=r["n"], unread=r["unread"]) for r in c.execute(q, params)]
    c.close(); return out


def _headers_rows(where, params, limit):
    c = _db()
    rows = c.execute(f"""SELECT e.id, e.server_id, e.folder_id, f.account_id, f.name folder, a.email account,
        e.sender, e.sender_address, e.subject, e.preview, e.date, e.unread, e.starred
        FROM emails e JOIN folders f ON e.folder_id=f.id JOIN accounts a ON f.account_id=a.id
        WHERE {where} ORDER BY e.date DESC LIMIT ?""", params + [limit]).fetchall()
    c.close()
    return [dict(id=r["id"], uid=r["server_id"], account=r["account"], folder=r["folder"],
                 sender=r["sender"], sender_address=r["sender_address"], subject=r["subject"],
                 preview=r["preview"], date=r["date"], unread=bool(r["unread"]), starred=bool(r["starred"])) for r in rows]


def tool_getRecentMessages(args):
    limit = int(args.get("limit", 20)); where = "1=1"; params = []
    if args.get("account"): where += " AND a.email=?"; params.append(args["account"])
    if args.get("folder"): where += " AND f.name=?"; params.append(args["folder"])
    if args.get("unread_only"): where += " AND e.unread=1"
    return _headers_rows(where, params, limit)


def tool_searchMessages(args):
    limit = int(args.get("limit", 30)); q = f"%{args.get('query','')}%"
    where = "(e.subject LIKE ? OR e.sender LIKE ? OR e.sender_address LIKE ? OR e.preview LIKE ?)"
    params = [q, q, q, q]
    if args.get("account"): where += " AND a.email=?"; params.append(args["account"])
    return _headers_rows(where, params, limit)


def _fetch_body_imap(account, uid, folder):
    cred = pc_secrets.credential_for(account)
    if cred is None:
        raise RuntimeError(f"sin credenciales para {account.email}")
    ctx = ssl.create_default_context()
    M = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, ssl_context=ctx)
    try:
        if cred.mechanism == MECHANISM_XOAUTH2:
            M.authenticate("XOAUTH2", lambda _c: xoauth2_response(cred.user, cred.secret).encode())
        else:
            M.login(cred.user, cred.secret)
        M.select(folder, readonly=True)
        typ, data = M.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data or data[0] is None:
            raise RuntimeError(f"UID {uid} no encontrado en {folder}")
        return data[0][1]
    finally:
        try: M.logout()
        except Exception: pass


def _text_from_raw(raw):
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = None
    if msg.is_multipart():
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            body = part.get_content()
            if part.get_content_type() == "text/html":
                body = re.sub(r"<[^>]+>", " ", body); body = re.sub(r"\s+\n", "\n", body)
    else:
        body = msg.get_content()
    hdr = {k: str(msg.get(k, "")) for k in ("From", "To", "Cc", "Subject", "Date")}
    return hdr, (body or "").strip()


def tool_getMessage(args):
    accs = _accounts()
    c = _db()
    r = c.execute("""SELECT e.server_id, f.account_id, f.name FROM emails e JOIN folders f ON e.folder_id=f.id
                     WHERE e.id=?""", [args["id"]]).fetchone()
    c.close()
    if r is None:
        return {"error": f"no hay un correo con id {args['id']} en la base de Postcard"}
    acc = accs.get(r["account_id"])
    raw = _fetch_body_imap(acc, r["server_id"], r["name"])
    hdr, body = _text_from_raw(raw)
    return {"account": acc.email, "folder": r["name"], "headers": hdr, "body": body[:20000]}


TOOLS = {
    "listAccounts": (tool_listAccounts, "Lista las 8 cuentas de Postcard con sus no leídos en bandeja de entrada.", {"type": "object", "properties": {}}),
    "listFolders": (tool_listFolders, "Carpetas de una cuenta (o de todas) con su número de correos y no leídos.",
                    {"type": "object", "properties": {"account_id": {"type": "integer"}}}),
    "getRecentMessages": (tool_getRecentMessages, "Correos recientes (cabeceras) por fecha; filtra por account (email), folder y unread_only.",
                          {"type": "object", "properties": {"account": {"type": "string"}, "folder": {"type": "string"}, "unread_only": {"type": "boolean"}, "limit": {"type": "integer"}}}),
    "searchMessages": (tool_searchMessages, "Busca en asunto/remitente/vista previa de lo descargado. Devuelve cabeceras.",
                       {"type": "object", "properties": {"query": {"type": "string"}, "account": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}),
    "getMessage": (tool_getMessage, "Cuerpo completo de un correo (baja el mensaje por IMAP por su id de la base de Postcard).",
                   {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}),
}


def handle(req):
    m = req.get("method"); rid = req.get("id")
    if m == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}}, "serverInfo": {"name": "postcard-mail", "version": "1.0"}}}
    if m == "notifications/initialized":
        return None
    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": n, "description": d, "inputSchema": s} for n, (_f, d, s) in TOOLS.items()]}}
    if m == "tools/call":
        p = req.get("params", {}); name = p.get("name"); args = p.get("arguments", {})
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"herramienta desconocida: {name}"}}
        try:
            res = TOOLS[name][0](args)
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": f"error: {type(e).__name__}: {e}"}], "isError": True}}
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"método desconocido: {m}"}}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n"); sys.stdout.flush()


if __name__ == "__main__":
    main()
