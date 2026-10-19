import email
import imaplib
import json
import os
import re
import smtplib
import time
from email.header import decode_header
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
from geeked import Geeked
import geeked.sign as geeked_sign

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
# 验证码：站点现在只下发需要交互识别的类型，用本地 word 求解器 + 反复换图重试
MAX_CAPTCHA_LOADS = int(os.environ.get('MAX_CAPTCHA_LOADS') or '20')
MAX_CAPTCHA_SOLVES = int(os.environ.get('MAX_CAPTCHA_SOLVES') or '5')
CAPTCHA_ATTEMPT_INTERVAL = float(os.environ.get('CAPTCHA_ATTEMPT_INTERVAL') or '2')
# 站点发不出验证码邮件时，请它重发的次数与间隔
MAX_EMAIL_RESENDS = int(os.environ.get('MAX_EMAIL_RESENDS') or '3')
EMAIL_RESEND_INTERVAL = float(os.environ.get('EMAIL_RESEND_INTERVAL') or '10')
# 反向邮箱验证（站点发不出验证码邮件时，用邮箱给站点发信）所需的 SMTP
SMTP_HOST = os.environ.get('SMTP_HOST') or ''
SMTP_PORT = int(os.environ.get('SMTP_PORT') or '465')
REVERSE_VERIFY_CHECKS = int(os.environ.get('REVERSE_VERIFY_CHECKS') or '30')
REVERSE_VERIFY_INTERVAL = float(os.environ.get('REVERSE_VERIFY_INTERVAL') or '3')

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

# 常见邮箱的 SMTP 服务器（反向邮箱验证时用账号邮箱给站点发信）
SMTP_SERVERS = {
    'qq.com': 'smtp.qq.com',
    'vip.qq.com': 'smtp.qq.com',
    'foxmail.com': 'smtp.qq.com',
    '163.com': 'smtp.163.com',
    '126.com': 'smtp.126.com',
    'yeah.net': 'smtp.yeah.net',
    'gmail.com': 'smtp.gmail.com',
    'googlemail.com': 'smtp.gmail.com',
    'outlook.com': 'smtp.office365.com',
    'hotmail.com': 'smtp.office365.com',
    'live.com': 'smtp.office365.com',
    'yahoo.com': 'smtp.mail.yahoo.com',
    'icloud.com': 'smtp.mail.me.com',
    'sina.com': 'smtp.sina.com',
    'sohu.com': 'smtp.sohu.com',
    '139.com': 'smtp.139.com',
    'aliyun.com': 'smtp.aliyun.com',
}

EMAIL_CODE_PATTERN = re.compile(r'(?<!\d)(\d{8})(?!\d)')
EMAIL_CODE_KEYWORDS = ('验证码', 'verification code', 'login code', 'code')


def guess_imap_host(mailbox):
    """根据邮箱域名猜测 IMAP 服务器地址。"""
    domain = mailbox.rsplit('@', 1)[-1].lower()
    return IMAP_SERVERS.get(domain, 'imap.{}'.format(domain))


def guess_smtp_host(mailbox):
    """根据邮箱域名猜测 SMTP 服务器地址。"""
    domain = mailbox.rsplit('@', 1)[-1].lower()
    return SMTP_SERVERS.get(domain, 'smtp.{}'.format(domain))


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


def trace_captcha_errors():
    """验证码接口解析失败时打印原始返回，避免只看到 KeyError: 'data'。"""
    original = Geeked.format_response
    if getattr(original, '_trace_installed', False):
        return

    def traced(self, response):
        try:
            return original(self, response)
        except Exception:
            print('验证码接口返回异常，原始内容: {}'.format(response[:400]))
            raise

    traced._trace_installed = True
    Geeked.format_response = traced


class ClickSolver:
    """把本地求解器的坐标接到 geeked 的「点击类」载荷分支上。

    geeked 根据 risk_type 决定 w 载荷的结构，点击类走的是 icon 分支，
    所以这里把它内部的 IconSolver 换成我们自己的实现。
    mode 由 solve_captcha 按实际下发的类型设置（word / icon）。
    """

    mode = 'word'

    def __init__(self, imgs, ques):
        self.imgs = imgs
        self.ques = list(ques or [])

    def find_icon_position(self):
        import gt4_word
        return gt4_word.solve_from_urls(self.imgs, self.ques, mode=ClickSolver.mode)


def solve_captcha():
    """反复取验证码，拿到 word/icon 类型就本地求解，失败就换一张重试。"""
    try:
        import gt4_word  # noqa: F401
    except Exception as ex:
        print('本地求解器不可用（{}），回退无感模式'.format(ex))
        return Geeked(CAPTCHA_ID, 'ai').solve()

    trace_captcha_errors()
    geeked_sign.IconSolver = ClickSolver
    loads = 0
    solves = 0
    while loads < MAX_CAPTCHA_LOADS and solves < MAX_CAPTCHA_SOLVES:
        loads += 1
        try:
            client = Geeked(CAPTCHA_ID, 'icon')
            data = client.load_captcha()
        except Exception as ex:
            print('第 {} 张验证码获取失败: {}'.format(loads, ex))
            time.sleep(CAPTCHA_ATTEMPT_INTERVAL)
            continue
        captcha_type = data.get('captcha_type')
        # 万一站点改回无感验证（不下发 icon/nine/word 等交互类型），
        # 仍按原来的无感方式提交这一张，不必等重试耗尽
        if captcha_type in (None, 'ai', 'invisible'):
            solves += 1
            try:
                # lot_number 与 load 时的 risk_type 绑定，必须用 ai 身份重新取一张再提交，
                # 否则服务端会回 -50303 illegal lot_number
                seccode = Geeked(CAPTCHA_ID, 'ai').solve()
                print('无感验证通过（第 {} 张）'.format(loads))
                return seccode
            except Exception as ex:
                print('第 {} 张无感提交未通过: {}'.format(loads, str(ex)[:160]))
                time.sleep(CAPTCHA_ATTEMPT_INTERVAL)
                continue
        if captcha_type not in ('word', 'icon'):
            print('第 {} 张是 {} 类型，换一张...'.format(loads, captcha_type))
            time.sleep(CAPTCHA_ATTEMPT_INTERVAL)
            continue
        solves += 1
        ClickSolver.mode = captcha_type
        client.lot_number = data['lot_number']
        try:
            seccode = client.submit_captcha(data)
            print('验证码通过（第 {} 张，{} 类型，第 {} 次求解）'.format(loads, captcha_type, solves))
            return seccode
        except Exception as ex:
            print('第 {} 次求解未通过: {}'.format(solves, str(ex)[:160]))
            time.sleep(CAPTCHA_ATTEMPT_INTERVAL)
    print('验证码求解失败（共取 {} 张，尝试求解 {} 次）'.format(loads, solves))
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
    typ, data = imap.uid('FETCH', msg_id, '(BODY.PEEK[])')
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


def scan_folder(imap, folder, since, checked, not_before, require_sender,
                min_uid=0, skip_codes=None):
    """在指定邮件目录里查找本次登录的验证码。

    min_uid 之前的邮件属于上一轮登录，直接跳过，避免读到旧验证码。
    """
    skip_codes = skip_codes or set()
    typ, _ = imap.select(folder, readonly=True)
    if typ != 'OK':
        return None
    typ, data = imap.uid('SEARCH', None, 'SINCE', since)
    uids = data[0].split() if data and data[0] else []
    for uid in reversed(uids[-40:]):
        if min_uid and int(uid) <= min_uid:
            continue
        key = (folder, uid, require_sender)
        if key in checked:
            continue
        checked.add(key)
        code = code_from_message(imap, uid, not_before, require_sender)
        if code and code not in skip_codes:
            return code
    return None


def mailbox_baseline(user):
    """记录各邮件目录当前的最大 UID，用于区分本轮新邮件与上一轮遗留邮件。"""
    if not MAIL_AUTHCODE:
        return {}
    host = IMAP_HOST or guess_imap_host(user)
    baseline = {}
    imap = None
    try:
        imap = open_mailbox(host, user, MAIL_AUTHCODE)
        folders = [IMAP_FOLDER]
        for folder in junk_folders(imap):
            if folder not in folders:
                folders.append(folder)
        for folder in folders:
            typ, _ = imap.select(folder, readonly=True)
            if typ != 'OK':
                continue
            typ, data = imap.uid('SEARCH', None, 'ALL')
            uids = data[0].split() if data and data[0] else []
            baseline[folder] = max(int(u) for u in uids) if uids else 0
    except Exception as ex:
        print('读取邮箱基线失败（不影响使用）: {}'.format(ex))
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass
    return baseline


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


def fetch_email_code(mailbox, not_before, baseline=None, skip_codes=None):
    """轮询邮箱，读取本次登录的 8 位邮箱验证码。

    baseline：各目录在本轮登录前的最大 UID，比它小的邮件一律不算；
    skip_codes：已经提交过且被站点拒绝的验证码，不再重复提交。
    """
    if not MAIL_AUTHCODE:
        print('站点要求邮箱验证码，但未配置 MAIL_AUTHCODE（邮箱授权码/应用密码）')
        return None

    baseline = baseline or {}
    skip_codes = skip_codes or set()
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
                    code = scan_folder(imap, folder, since, checked, not_before, require_sender,
                                       int(baseline.get(folder, 0)), skip_codes)
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


def send_mail(sender, recipient, subject, body):
    """用账号邮箱给站点发信（反向邮箱验证）。"""
    from email.mime.text import MIMEText

    message = MIMEText(body, 'plain', 'utf-8')
    message['Subject'] = subject
    message['From'] = sender
    message['To'] = recipient
    host = SMTP_HOST or guess_smtp_host(sender)
    if SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(host, SMTP_PORT, timeout=30)
    else:
        server = smtplib.SMTP(host, SMTP_PORT, timeout=30)
        server.starttls()
    try:
        server.login(sender, MAIL_AUTHCODE)
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def reverse_email_verify(session, header, user):
    """站点发不出验证码邮件时的备用验证方式。

    站点先给一个 verify_id 和收件地址，要求用账号邮箱给该地址发一封
    标题和内容都是 verify_id 的邮件；站点收到后即完成验证。
    """
    if not MAIL_AUTHCODE:
        print('未配置 MAIL_AUTHCODE，无法使用反向邮箱验证')
        return None
    sender = MAIL_USER or user
    host = urlparse(url).netloc
    response = post_login(session, header, {
        'host': host,
        'phase': 'reverse_email_verify',
        'reverse_email_verify_action': 'start',
    })
    if response.get('result') != 'reverse_email_verify_started':
        print('反向邮箱验证未能开始: {}'.format(response.get('msg', response)))
        return None
    verify_id = response.get('verify_id')
    recipient = response.get('recipient')
    if not verify_id or not recipient:
        print('反向邮箱验证缺少 verify_id / recipient')
        return None
    print('向 {} 发送验证邮件（标题与内容为验证串）...'.format(recipient))
    try:
        send_mail(sender, recipient, verify_id, verify_id)
        print('验证邮件已发送')
    except Exception as ex:
        print('验证邮件发送失败: {}'.format(ex))
        return None
    for attempt in range(1, REVERSE_VERIFY_CHECKS + 1):
        time.sleep(REVERSE_VERIFY_INTERVAL)
        response = post_login(session, header, {
            'host': host,
            'phase': 'reverse_email_verify',
            'reverse_email_verify_action': 'check',
            'reverse_email_verify_id': verify_id,
        })
        result = response.get('result')
        if result == 'reverse_email_verify_verified':
            print('站点已收到验证邮件（第 {} 次检查）'.format(attempt))
            return post_login(session, header, {
                'host': host,
                'phase': 'reverse_email_verify',
                'reverse_email_verify_action': 'finish',
                'reverse_email_verify_id': verify_id,
            })
        if result not in ('reverse_email_verify_pending', None):
            print('反向邮箱验证异常: {}'.format(response.get('msg', response)))
            return None
    print('反向邮箱验证超时（{} 次检查）'.format(REVERSE_VERIFY_CHECKS))
    return None


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

    # 先记下邮箱当前状态，之后只认本轮新增的邮件，避免读到上一轮的验证码
    baseline = mailbox_baseline(MAIL_USER or user)
    response = post_login(session, header, build_login_data(user, pwd, captcha, page_loaded_at))
    code_tries = 0
    resend_tries = 0
    tried_codes = set()
    while not login_succeeded(response):
        if response.get('phase') == 'reverse_email_verify':
            print('改用反向邮箱验证：用邮箱给站点发信...')
            verified = reverse_email_verify(session, header, user)
            if verified is None:
                return False, '反向邮箱验证未通过'
            response = verified
            continue
        if response.get('phase') != 'email_code':
            break
        # 站点没把邮件发出去（code_sent=false）时，请它重发，而不是干等邮箱
        if response.get('code_sent') is False or response.get('result') == 'email_send_failed':
            if resend_tries >= MAX_EMAIL_RESENDS:
                print('站点始终未能发出验证码邮件，尝试反向邮箱验证...')
                verified = reverse_email_verify(session, header, user)
                if verified is None:
                    return False, '站点发不出邮箱验证码，反向邮箱验证也未通过'
                response = verified
                continue
            resend_tries += 1
            print('站点未能发出验证码邮件，请求重发（第 {} 次）...'.format(resend_tries))
            time.sleep(EMAIL_RESEND_INTERVAL)
            response = post_login(session, header, {
                'host': urlparse(url).netloc,
                'phase': 'email_resend',
            })
            continue
        if code_tries >= MAX_EMAIL_CODE_TRIES:
            break
        code = fetch_email_code(user, started_at, baseline, tried_codes)
        if not code:
            print('邮箱里没等到验证码，尝试反向邮箱验证...')
            verified = reverse_email_verify(session, header, user)
            if verified is None:
                return False, '站点要求登录邮箱验证码，未能自动读取到验证码'
            response = verified
            continue
        code_tries += 1
        tried_codes.add(code)
        response = post_login(session, header, {
            'host': urlparse(url).netloc,
            'phase': 'email_code',
            'email_code': code,
        })
        if response.get('result') == 'invalid_code':
            print('站点判定这个验证码不对（可能是上一轮的旧邮件），继续等新邮件...')

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
