# email_sender.py

import os
import smtplib

from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid


class HtmlEmailSender:

    def __init__(
            self,
            smtp_server,
            smtp_port,
            sender_email,
            sender_name=None
    ):
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.sender_email = sender_email
        self.sender_name = sender_name

    def _from_header(self):
        # 中文发件人名需要 RFC 2047 编码，否则 Outlook 里会显示乱码
        if not self.sender_name:
            return self.sender_email
        return "{} <{}>".format(
            Header(self.sender_name, "utf-8").encode(),
            self.sender_email
        )

    def send_html_file(
            self,
            html_file,
            subject,
            receivers,
            cc=None
    ):

        if cc is None:
            cc = []

        if not os.path.exists(html_file):
            raise FileNotFoundError(
                f"HTML文件不存在: {html_file}"
            )

        with open(
                html_file,
                "r",
                encoding="utf-8"
        ) as f:

            html_content = f.read()

        msg = MIMEMultipart("alternative")

        msg["From"] = self._from_header()
        msg["To"] = ",".join(receivers)

        if cc:
            msg["Cc"] = ",".join(cc)

        # 主题含中文，必须编码成 =?utf-8?b?...?= ，否则部分邮件网关会截断或乱码
        msg["Subject"] = Header(subject, "utf-8")
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.sender_email.split("@")[-1])

        msg.attach(
            MIMEText(
                html_content,
                "html",
                "utf-8"
            )
        )

        all_receivers = receivers + cc

        try:

            smtp = smtplib.SMTP(
                self.smtp_server,
                self.smtp_port
            )

            smtp.sendmail(
                self.sender_email,
                all_receivers,
                msg.as_string()
            )

            smtp.quit()

            print(
                f"邮件发送成功: "
                f"{','.join(all_receivers)}"
            )

        except Exception as e:

            print(
                f"邮件发送失败: {e}"
            )
            raise
