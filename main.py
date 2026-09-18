import email
import imaplib
import json
import os
import re
import time
from email.header import decode_header
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
from geeked import Geeked

# 机场的地址
url = os.environ.get('URL')
# 配置用户名（一般是邮箱）
config = os.environ.get('CONFIG')
# server酱
SCKEY = os.environ.get('SCKEY')

# 邮箱 IMAP 配置：站点要求登录邮箱验证码时，用它自动读取邮件里的 8 位验证码
MAIL_AUTHCODE = os.environ.get('MAIL_AUTHCODE') or ''
MAIL_USER = os.environ.get('MAIL_USER') or ''
MAIL_SENDER = os.environ.get('MAIL_SENDER') or 'no-reply@ikuuu.cc'
IMAP_HOST = os.environ.get('IMAP_HOST') or ''
IMAP_PORT = int(os.environ.get('IMAP_PORT') or '993')
IMAP_FOLDER = os.environ.get('IMAP_FOLDER') or 'INBOX'
EMAIL_CODE_TIMEOUT = int(os.environ.get('EMAIL_CODE_TIMEOUT') or '180')
EMAIL_CODE_POLL_INTERVAL = int(os.environ.get('EMAIL_CODE_POLL_INTERVAL') or '5')
# 在这个时间内只认发件人匹配的邮件，超时后再放宽，兼容站点更换发信地址
MAIL_SENDER_RELAX_SECONDS = 60

# GeeTest V4 验证码 ID（从 ikuuu.org 提取）
CAPTCHA_ID = 'cc96d05ba8b60f9112f76e18526fcb73'

login_url = '{}/auth/login'.format(url)
check_url = '{}/user/checkin'.format(url)

MAX_RETRIES = 3
MAX_EMAIL_CODE_TRIES = 2

# 常见邮箱的 IMAP 服务器，用于自动识别
IMAP_SERVERS = {
    'qq.com': 'imap.qq.com',
    'vip.qq.com': 'imap.qq.com',
    'foxmail.com': 'imap.qq.com',
    '163.com': 'imap.163.com',
    '126.com': 'imap.126.com',
    'yeah.net': 'imap.yeah.net',
    'gmail.com': 'imap.gmail.com',
    'googlemail.com': 'imap.gmail.com',
    'outlook.com': 'outlook.office365.com',
    'hotmail.com': 'outlook.office365.com',
    'live.com': 'outlook.office365.com',
    'yahoo.com': 'imap.mail.yahoo.com',
    'icloud.com': 'imap.mail.me.com',
    'sina.com': 'imap.sina.com',
    'sohu.com': 'imap.sohu.com',
    '139.com': 'imap.139.com',
    'aliyun.com': 'imap.aliyun.com',
}

EMAIL_CODE_PATTERN = re.compile(r'(?<!\d)(\d{8})(?!\d)')
EMAIL_CODE_KEYWORDS = ('验证码', 'verification code', 'login code', 'code')


def guess_imap_host(mailbox):
    """根据邮箱域名猜测 IMAP 服务器地址。"""
    domain = mailbox.rsplit('@', 1)[-1].lower()
    return IMAP_SERVERS.get(domain, 'imap.{}'.format(domain))


def build_login_data(user, pwd, captcha, page_loaded_at):
    """构造同时兼容新版分阶段登录和旧版登录接口的表单。"""
    return {
        'host': urlparse(url).netloc,
        'phase': 'password',
        'email': user,
        'passwd': pwd,
        'pageLoadedAt': page_loaded_at,
        'captcha_result[lot_number]': captcha['lot_number'],
        'captcha_result[captcha_output]': captcha['captcha_output'],
        'captcha_result[pass_token]': captcha['pass_token'],
        'captcha_result[gen_time]': captcha['gen_time'],
    }


def login_succeeded(response):
    """兼容新版 phase 响应和旧版 ret 响应。"""
    return (
        response.get('phase') == 'authenticated'
        or str(response.get('ret')) == '1'
    )


def solve_captcha():
    """使用 GeekedTest 求解 GeeTest V4 验证码（纯 Python，无需浏览器），支持重试"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f'验证码求解尝试 {attempt}/{MAX_RETRIES}...')
            geeked = Geeked(CAPTCHA_ID, 'ai')
            result = geeked.solve()
            print('验证码求解成功')
            return result
        except Exception as ex:
            print(f'验证码求解失败 (尝试 {attempt}/{MAX_RETRIES}): {ex}')
            if attempt < MAX_RETRIES:
                wait = 2
                print(f'等待 {wait} 秒后重试...')
                time.sleep(wait)
    print('所有重试均失败')
    return None


def decode_header_value(value):
    """解码邮件头里的编码文字，例如 =?UTF-8?B?...?=。"""
    if not value:
        return ''
    parts = []
    for text, charset in decode_header(value):
        if isinstance(text, bytes):
            parts.append(text.decode(charset or 'utf-8', errors='ignore'))
        else:
            parts.append(text)
    return ''.join(parts)


def message_text(message):
    """取出邮件正文（纯文本或 HTML）。"""
    chunks = []
    for part in message.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        if part.get_content_type() not in ('text/plain', 'text/html'):
            continue
        try:
            payload = part.get_payload(decode=True) or b''
            chunks.append(payload.decode(part.get_content_charset() or 'utf-8', errors='ignore'))
        except Exception:
            continue
    return '\n'.join(chunks)


def extract_email_code(text):
    """从邮件内容中提取 8 位登录验证码，优先取带“验证码”等上下文的数字。"""
    if not text:
        return None
    for keyword in EMAIL_CODE_KEYWORDS:
        for match in re.finditer(re.escape(keyword), text, re.IGNORECASE):
            window = text[max(0, match.start() - 120):match.end() + 160]
            found = EMAIL_CODE_PATTERN.search(window)
            if found:
                return found.group(1)
    codes = EMAIL_CODE_PATTERN.findall(text)
    return codes[0] if codes else None


def message_time(message):
    """返回邮件的发送时间戳，解析失败时返回 None。"""
    try:
        return parsedate_to_datetime(message.get('Date')).timestamp()
    except Exception:
        return None


def sender_tokens():
    """用来判断邮件是否来自本站：默认发件人 + 站点域名（含二级域名）。"""
    tokens = []
    if MAIL_SENDER:
        tokens.append(MAIL_SENDER.lower())
    host = urlparse(url).netloc.lower().split(':')[0] if url else ''
    if host:
        tokens.append(host)
        label = host.split('.')[0]
        # 站点域名和发信域名不一定相同，例如 ikuuu.org 用 no-reply@ikuuu.cc 发信
        if len(label) >= 4:
            tokens.append(label)
    return [t for t in tokens if t]


def sender_matches(message):
    """检查邮件的 From 是否属于本站。"""
    tokens = sender_tokens()
    if not tokens:
        return True
    sender = decode_header_value(message.get('From')).lower()
    return any(token in sender for token in tokens)


def code_from_message(imap, msg_id, not_before, require_sender=True):
    """读取单封邮件并提取验证码，忽略发送时间过旧或发件人不符的邮件。"""
    typ, data = imap.fetch(msg_id, '(BODY.PEEK[])')
    if typ != 'OK' or not data or not isinstance(data[0], tuple):
        return None
    message = email.message_from_bytes(data[0][1])
    sent_at = message_time(message)
    # 只接受本次登录前后收到的邮件，避免误用上次登录的旧验证码
    if sent_at is not None and sent_at < not_before - 300:
        return None
    if require_sender and not sender_matches(message):
        return None
    subject = decode_header_value(message.get('Subject'))
    return extract_email_code(subject + '\n' + message_text(message))


def junk_folders(imap):
    """通过 IMAP 的 \\Junk / \\Spam 标记找出垃圾邮件目录，避免验证码被误拦。"""
    names = []
    try:
        typ, data = imap.list()
    except Exception:
        return names
    if typ != 'OK':
        return names
    for raw in data or []:
        line = raw.decode('utf-8', errors='ignore') if isinstance(raw, bytes) else str(raw)
        if not re.search(r'\\(Junk|Spam)\b', line, re.IGNORECASE):
            continue
        match = re.search(r'"([^"]*)"\s*$', line.strip())
        name = match.group(1) if match else line.split()[-1].strip('"')
        if name and name not in names:
            names.append(name)
    return names


def scan_folder(imap, folder, since, checked, not_before, require_sender):
    """在指定邮件目录里查找本次登录的验证码。"""
    typ, _ = imap.select(folder, readonly=True)
    if typ != 'OK':
        return None
    typ, data = imap.search(None, 'SINCE', since)
    msg_ids = data[0].split() if data and data[0] else []
    for msg_id in reversed(msg_ids[-40:]):
        key = (folder, msg_id, require_sender)
        if key in checked:
            continue
        checked.add(key)
        code = code_from_message(imap, msg_id, not_before, require_sender)
        if code:
            return code
    return None


def open_mailbox(host, user, authcode):
    """连接 IMAP 邮箱；网易邮箱需要先发送 ID 命令，否则会报 Unsafe Login。"""
    imap = imaplib.IMAP4_SSL(host, IMAP_PORT, timeout=30)
    imap.login(user, authcode)
    try:
        imap.xatom('ID', '("name" "imaplib" "version" "1.0" "vendor" "python")')
    except Exception:
        pass
    typ, _ = imap.select(IMAP_FOLDER, readonly=True)
    if typ != 'OK':
        raise RuntimeError('无法打开邮件目录 {}'.format(IMAP_FOLDER))
    return imap


def fetch_email_code(mailbox, not_before):
    """轮询邮箱，读取本次登录的 8 位邮箱验证码。"""
    if not MAIL_AUTHCODE:
        print('站点要求邮箱验证码，但未配置 MAIL_AUTHCODE（邮箱授权码/应用密码）')
        return None

    user = MAIL_USER or mailbox
    host = IMAP_HOST or guess_imap_host(user)
    deadline = time.time() + EMAIL_CODE_TIMEOUT
    print('登录需要邮箱验证码，正在通过 IMAP 读取 {} 的邮件...'.format(host))

    checked = set()
    since = time.strftime('%d-%b-%Y', time.localtime(not_before - 86400))
    folders = [IMAP_FOLDER]
    junk_loaded = False
    imap = None
    try:
        # 保持一条 IMAP 连接轮询，避免反复登录被邮箱服务商限流
        while time.time() < deadline:
            try:
                if imap is None:
                    imap = open_mailbox(host, user, MAIL_AUTHCODE)
                if not junk_loaded:
                    junk_loaded = True
                    for folder in junk_folders(imap):
                        if folder not in folders:
                            folders.append(folder)
                            print('同时检查垃圾邮件目录: {}'.format(folder))
                # 先只认本站发件人；过了等待窗口仍没收到，再放宽到全部邮件
                require_sender = (time.time() - not_before) < MAIL_SENDER_RELAX_SECONDS
                for folder in folders:
                    code = scan_folder(imap, folder, since, checked, not_before, require_sender)
                    if code:
                        print('已读取到邮箱验证码')
                        return code
                print('暂未收到验证码邮件，{} 秒后重试...'.format(EMAIL_CODE_POLL_INTERVAL))
            except Exception as ex:
                print('读取邮箱失败: {}，稍后重新连接'.format(ex))
                if imap is not None:
                    try:
                        imap.logout()
                    except Exception:
                        pass
                    imap = None
            time.sleep(EMAIL_CODE_POLL_INTERVAL)
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass

    print('等待邮箱验证码超时（{} 秒）'.format(EMAIL_CODE_TIMEOUT))
    return None


def post_login(session, header, data):
    """向登录接口提交一个阶段，返回解析后的 JSON。"""
    response = session.post(url=login_url, headers=header, data=data, timeout=30)
    response.raise_for_status()
    print(response.text)
    try:
        return response.json()
    except ValueError:
        return {'msg': '登录接口返回了非 JSON 内容: {}'.format(response.text[:200])}


def login(session, header, user, pwd):
    """完成分阶段登录，返回 (是否成功, 提示信息)。"""
    # 先访问登录页，建立与浏览器一致的会话，并记录页面加载时间。
    login_page = session.get(url=login_url, headers=header, timeout=30)
    login_page.raise_for_status()
    started_at = time.time()
    page_loaded_at = int(started_at * 1000)

    # 求解 GeeTest V4 验证码
    captcha = solve_captcha()
    if captcha is None:
        return False, '验证码求解失败，跳过此账号'

    response = post_login(session, header, build_login_data(user, pwd, captcha, page_loaded_at))
    for _ in range(MAX_EMAIL_CODE_TRIES):
        if login_succeeded(response):
            return True, response.get('msg', '登录成功')
        if response.get('phase') != 'email_code':
            break
        code = fetch_email_code(user, started_at)
        if not code:
            return False, '站点要求登录邮箱验证码，未能自动读取到验证码'
        response = post_login(session, header, {
            'host': urlparse(url).netloc,
            'phase': 'email_code',
            'email_code': code,
        })

    if login_succeeded(response):
        return True, response.get('msg', '登录成功')
    return False, response.get('msg', '登录接口未返回提示信息')


def push(content):
    """通过 Server酱 推送结果。"""
    if SCKEY != '':
        push_url = 'https://sctapi.ftqq.com/{}.send?title=机场签到&desp={}'.format(SCKEY, content)
        requests.post(url=push_url)
        print('推送成功')


def sign(order, user, pwd):
    session = requests.session()
    header = {
        'origin': url,
        'referer': login_url,
        'x-requested-with': 'XMLHttpRequest',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36'
    }
    try:
        print(f'===账号{order}进行登录...===')
        print(f'账号：{user}')

        ok, message = login(session, header, user, pwd)
        print(message)
        if not ok:
            print(f'登录失败: {message}')
            push(f'登录失败: {message}')
            return

        # 进行签到
        res2 = session.post(url=check_url, headers=header, timeout=30).text
        print(res2)
        result = json.loads(res2)
        print(result['msg'])
        push(result['msg'])
    except Exception as ex:
        print('签到失败')
        print("出现如下异常%s" % ex)
        push('签到失败')
    print('===账号{order}签到结束===\n'.format(order=order))


if __name__ == '__main__':
    configs = config.splitlines()
    if len(configs) % 2 != 0 or len(configs) == 0:
        print('配置文件格式错误')
        exit()
    user_quantity = len(configs)
    user_quantity = user_quantity // 2
    for i in range(user_quantity):
        user = configs[i * 2]
        pwd = configs[i * 2 + 1]
        sign(i, user, pwd)
