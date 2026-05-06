---
name: get-leetcode-page
description: 在给定 LeetCode 题号，需要获取 LeetCode 题目的网页内容时执行，生成 Markdown 格式的题目内容文件
argument-hint: <problem-id>
allowed-tools:
  - WebFetch
---

基于第三方跳转服务 lcid.cc 找到相应的 LeetCode 题目的网页，具体来说，题号为 $0 的题目网页为 https://lcid.cc/cn/$0 。

如果在跳转后可以正常得到题目内容，则从网页中提取题目内容，将其保存为 Markdown 格式的文件。

如果在跳转后无法得到题目内容（很可能是会员专属题目），则放弃生成 Markdown 文件。