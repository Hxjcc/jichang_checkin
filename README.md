# 通用机场签到😍<br/>
>只要机场网站''' Powered by SSPANEL ''',就可以进行签到。要确认是否是''' Powered by SSPANEL '''，在机场首页滑倒最底端就可以看到。例如：
![Y0}SY$J`8837H8T5GXM1DZY](https://user-images.githubusercontent.com/21276183/214764546-4f66333a-cb9b-420e-8260-697d26fb4547.png)
## 作用
>每天进行签到，获取额外的流量奖励

## 推送方式
  🚀🚀该脚本采用的是<a href = 'https://sct.ftqq.com/r/5126'>Server酱</a>的推送方式，如果不需要推送，就下面的SCKEY参数的值设置为<b>空</b>就行

# 部署过程
 
1. 右上角Fork此仓库
2. 然后到`Settings`→`Secrets and variables`→`Actions` 新建以下机密：

| 参数   | 是否必须  | 内容  | 
| ------------ | ------------ | ------------ |
| CONFIG| 是  | 账号密码  |
| SCKEY  | 否  | Sever酱秘钥  |
| MAIL_AUTHCODE | 否* | 邮箱授权码/应用密码，用于自动读取登录邮箱验证码 |
| MAIL_USER | 否  | 邮箱账号，默认与登录账号相同 |
| IMAP_HOST | 否  | IMAP 服务器地址，一般可自动识别，识别不了时手动填写 |
| MAIL_SENDER | 否  | 验证码邮件的发件人，默认 no-reply@ikuuu.cc |
<br/>
<b>其中URL的值必须是机场网站的地址，例如：https://example.com</b>,尾部不要加''' / '''号 config写法：一行账号一行密码

3. 到`Actions`中创建一个workflow，运行一次，以后每天项目都会自动运行。<br/>
4. 最后，可以到Run sign查看签到情况，同时也会也会将签到详情推送到Sever酱。

## 登录邮箱验证码（重要）

iKuuu 在 2026 年 9 月新增了登录邮箱验证：当登录来自陌生 IP（例如 GitHub Actions 的美国 IP）时，
站点会向账号邮箱发送一个 8 位验证码，要求验证后才能登录，日志表现为：

```text
{"phase":"email_code","result":"verification_required","msg":"为保护账号安全，本次登录需要邮箱验证..."}
```

此时脚本无法只靠账号密码登录，需要配置 `MAIL_AUTHCODE`，让它连上邮箱（IMAP）自动读取这个验证码：

1. 登录你的邮箱网页版，在设置中开启 **IMAP/SMTP 服务**
2. 生成一个 **授权码**（QQ/163/126 叫授权码，Gmail 叫应用专用密码），注意不是网页登录密码
3. 把授权码填到仓库的 `MAIL_AUTHCODE` 机密里

支持的邮箱会自动识别 IMAP 服务器：QQ/Foxmail、163、126、yeah.net、Gmail、Outlook/Hotmail、
Yahoo、iCloud、Sina、Sohu、139、阿里云邮箱。其他邮箱需要额外填写 `IMAP_HOST`。

脚本判断"哪封是新邮件"的规则是：**发件人是 `no-reply@ikuuu.cc`（或站点域名）**、
发送时间在本次登录前后，并且正文里有 8 位数字。实测邮件长这样：

```text
发件人：no-reply@ikuuu.cc
主题：  iKuuu VPN- 异常登录验证
正文：  以下8位数字是邮箱验证码，请在网站上填写以通过验证 …
        95887907
```

等 60 秒还没收到本站发件人的邮件时，才会放宽到"任意含 8 位数字的新邮件"，
以免站点更换发信地址后失效。脚本还会自动扫描垃圾邮件目录（IMAP `\Junk` / `\Spam` 标记）。

如果验证码邮件进了垃圾箱，可以把 `IMAP_FOLDER` 机密设为垃圾箱目录名；等待超时时间可用
`EMAIL_CODE_TIMEOUT` 调整（默认 180 秒）。

> 注意：实测从国内家庭宽带的 IP 登录**同样会触发**邮箱验证，所以"换国内 IP 就能绕过"并不可靠，
> 配置邮箱读取是目前唯一稳定的方案。
