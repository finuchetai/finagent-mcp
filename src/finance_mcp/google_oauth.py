"""Owner Google OAuth for Sheets: browser consent, PKCE, OS credential storage.

Run login outside MCP stdio. Never launch a browser from a financial tool call.
"""
import argparse
import json
import os
from pathlib import Path
import re

from .contract import ContractError

READ_SCOPE = 'https://www.googleapis.com/auth/spreadsheets.readonly'
TOKEN_URI = 'https://oauth2.googleapis.com/token'
AUTH_URI = 'https://accounts.google.com/o/oauth2/auth'
SERVICE = 'finagent-mcp/google-sheets/v1'
SAFE_BACKENDS = {'keyring.backends.Windows', 'keyring.backends.macOS',
                 'keyring.backends.SecretService', 'keyring.backends.kwallet'}


def profile_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value):
        raise ContractError('Некорректное имя профиля Google')
    return value


def app_config(path=None):
    """Developer registers the app once; customers do not create Cloud projects."""
    source = path or os.environ.get('FINANCE_GOOGLE_OAUTH_APP')
    if not source:
        candidate = Path(__file__).parent / 'resources/google-oauth-client.json'
        if not candidate.exists():
            raise ContractError('Вход Google ещё не настроен автором комплекта: нужен OAuth Desktop client приложения. Клиенту создавать сервисный аккаунт не требуется.')
        source = candidate
    try:
        obj = json.loads(Path(source).read_text(encoding='utf-8'))
        config = obj['installed']
        if (not isinstance(config, dict) or
                not re.fullmatch(r'[A-Za-z0-9_-]+\.apps\.googleusercontent\.com', config['client_id']) or
                config['auth_uri'] != AUTH_URI or config['token_uri'] != TOKEN_URI or
                not isinstance(config.get('client_secret'), str)):
            raise ValueError('invalid app')
        # Ignore extra/untrusted endpoints; only Google OAuth is supported.
        return {'installed': {k: config[k] for k in ('client_id', 'client_secret', 'auth_uri', 'token_uri')}}
    except (OSError, ValueError, KeyError, TypeError):
        raise ContractError('Некорректная конфигурация приложения Google OAuth') from None


class TokenStore:
    def __init__(self, profile):
        self.profile = profile_name(profile)
        try:
            import keyring
            candidate = keyring.get_keyring()
            candidates = getattr(candidate, 'backends', [candidate])
            self.backend = next(b for b in candidates if type(b).__module__ in SAFE_BACKENDS)
        except Exception:
            raise ContractError('Недоступно защищённое хранилище паролей ОС. Агенту нужно настроить Windows Credential Manager, macOS Keychain или Linux Secret Service; токены в обычный файл не сохраняются.') from None

    def read(self):
        try:
            return self.backend.get_password(SERVICE, self.profile)
        except Exception:
            raise ContractError('Не удалось открыть защищённый профиль Google') from None

    def write(self, value):
        try:
            self.backend.set_password(SERVICE, self.profile, value)
        except Exception:
            raise ContractError('Не удалось сохранить разрешение Google в хранилище ОС') from None

    def delete(self):
        try:
            if self.read() is not None:
                self.backend.delete_password(SERVICE, self.profile)
        except ContractError:
            raise
        except Exception:
            raise ContractError('Не удалось удалить локальное разрешение Google') from None


def stored_credentials(profile='default', *, store=None, app=None):
    config = app
    raw = (store or TokenStore(profile)).read()
    if raw is None:
        raise ContractError('Сначала подключите Google: finance-mcp-google login. Затем перезапустите MCP.')
    try:
        from google.oauth2.credentials import Credentials
        obj = json.loads(raw)
        if (obj.get('token_uri') != TOKEN_URI or
                not re.fullmatch(r'[A-Za-z0-9_-]+\.apps\.googleusercontent\.com', obj.get('client_id', '')) or
                (config is not None and obj.get('client_id') != config['installed']['client_id']) or
                obj.get('scopes') != [READ_SCOPE] or
                not isinstance(obj.get('refresh_token'), str) or not obj['refresh_token']):
            raise ValueError('invalid token profile')
        return Credentials.from_authorized_user_info(obj, scopes=[READ_SCOPE])
    except ImportError:
        raise ContractError('Установите MCP с extra [sheets]') from None
    except Exception:
        raise ContractError('Разрешение Google недействительно; повторите подключение') from None


def authorized_oauth_session(profile='default'):
    try:
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        raise ContractError('Установите MCP с extra [sheets]') from None
    return AuthorizedSession(stored_credentials(profile), refresh_timeout=30)


def login(profile='default', *, app_path=None, store=None, flow_class=None):
    profile_name(profile)
    config = app_config(app_path)
    target = store or TokenStore(profile)  # Check secure storage before asking for consent.
    if flow_class is None:
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow_class = InstalledAppFlow
        except ImportError:
            raise ContractError('Установите MCP с extra [sheets]') from None
    print('Откроется Google. Разрешение — только чтение Google-таблиц. Google запрашивает доступ ко всем доступным вам таблицам; MCP читает только книгу из согласованной конфигурации. Пароль и коды в чат не отправляйте.')
    try:
        flow = flow_class.from_client_config(config, scopes=[READ_SCOPE], autogenerate_code_verifier=True)
        credentials = flow.run_local_server(
            host='127.0.0.1', port=0, open_browser=True, timeout_seconds=180,
            authorization_prompt_message='Если браузер не открылся, откройте на ЭТОМ ЖЕ компьютере: {url}',
            success_message='Ответ Google получен. Вернитесь к ассистенту: он проверит сохранение доступа и чтение таблицы.',
            access_type='offline', prompt='consent', include_granted_scopes='false')
        granted = credentials.granted_scopes or credentials.scopes
        if set(granted or []) != {READ_SCOPE} or not credentials.refresh_token:
            raise ContractError('Google не выдал нужное разрешение на чтение с обновлением; подключение не сохранено')
        # Validate serialized credentials before storing them; no tokens in stdout.
        raw = credentials.to_json()
        class Candidate:
            def read(self): return raw
        stored_credentials(profile, store=Candidate(), app=config)
        target.write(raw)
    except ContractError:
        raise
    except Exception:
        raise ContractError('Вход Google не завершён: отказ, таймаут или ошибка соединения. Можно повторить подключение; коды и токены в чат не отправляйте.') from None
    return {'status': 'connected', 'profile': profile, 'scope': 'Google Sheets, read-only'}


def disconnect(profile='default', *, store=None, app=None, session=None):
    target = store or TokenStore(profile)
    if target.read() is None:
        return {'status': 'not_connected'}
    credentials = stored_credentials(profile, store=target, app=app)
    try:
        import requests
        http = session or requests
        result = http.post('https://oauth2.googleapis.com/revoke',
                           data={'token': credentials.refresh_token}, timeout=30, allow_redirects=False)
        if result.status_code != 200:
            raise ContractError('Google не подтвердил отзыв доступа. Отключите приложение в настройках аккаунта Google и остановите MCP.')
        target.delete()
    except ContractError:
        raise
    except Exception:
        raise ContractError('Не удалось подтвердить отзыв Google. Отключите приложение в аккаунте Google и остановите MCP; локальный профиль сохранён для повторной попытки.') from None
    return {'status': 'disconnected', 'note': 'Остановите MCP, чтобы удалить снимок и активную сессию из памяти.'}


def main():
    parser = argparse.ArgumentParser(description='Подключение Google Sheets: вход в браузере без сервисного аккаунта')
    parser.add_argument('action', choices=['login', 'status', 'disconnect'])
    parser.add_argument('--profile', default='default')
    parser.add_argument('--app', help='Конфигурация OAuth Desktop приложения автора; не ключ сервисного аккаунта')
    args = parser.parse_args()
    try:
        if args.app:
            os.environ['FINANCE_GOOGLE_OAUTH_APP'] = str(Path(args.app).resolve())
        if args.action == 'login':
            result = login(args.profile)
        elif args.action == 'disconnect':
            result = disconnect(args.profile)
        else:
            credentials = stored_credentials(args.profile)
            result = {'status': 'permission_saved', 'profile': args.profile,
                      'note': 'Сохранённое разрешение не доказывает чтение таблицы; проверьте refresh_source.'}
        print(json.dumps(result, ensure_ascii=False))
    except ContractError as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
