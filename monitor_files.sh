#!/bin/bash

# 监控目录
DIR="/home/srv_cnexp_rpa/rpa/work"

# 接收邮箱（多个收件人用空格分隔）
MAIL_TO="kexue.guo@dhl.com user2@dhl.com user3@dhl.com"

# 抄送邮箱（多个抄送人用空格分隔）
MAIL_CC="cc1@dhl.com cc2@dhl.com"

# 临时文件
TMP_FILE="/home/srv_cnexp_rpa/bin/old_files.txt"

# 清空临时文件
> "$TMP_FILE"

# 查找1小时未修改的文件（mtime > 60分钟）
find "$DIR" -type f -mmin +60 > "$TMP_FILE"

# 判断是否有文件
if [ -s "$TMP_FILE" ]; then
    SUBJECT="【告警】目录存在超过1小时未处理文件"

    echo "以下文件超过1小时未处理：" > /tmp/mail_body.txt
    echo "" >> /tmp/mail_body.txt
    cat "$TMP_FILE" >> /tmp/mail_body.txt

    # 构造抄送参数（将空格分隔转换为逗号分隔）
    CC_LIST=$(echo "$MAIL_CC" | tr ' ' ',')
    TO_LIST=$(echo "$MAIL_TO" | tr ' ' ',')

    # 发送邮件（-c 指定抄送）
    mail -s "$SUBJECT" -c "$CC_LIST" "$TO_LIST" < /tmp/mail_body.txt
fi
