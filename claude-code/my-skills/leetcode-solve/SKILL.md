---
name: leetcode-solve
description: 基于给定 LeetCode 题号，用相应的编程语言解决该 LeetCode 题目
argument-hint: <problem-id> <language>
disable-model-invocation: true
allowed-tools:
  - Write
  - Bash
  - Update
---

使用编程语言 $1 完成题号为 $0 的 LeetCode 题目，具体实现如下:

1. 获取 LeetCode 题目 （优先考虑使用已有的 Skill）。

2. 检查 LeetCode 题目是否 Markdown 格式的文件生成，如果生成则使用该文件，并注意文件放置在正确的目录下；如果未生成，说明情况并放弃作答。

3. 根据题目要求，使用编程语言 $1 实现解决方案，同时实现测试用例，保存代码文件。

4. 测试代码实现是否正确，并给出解决方案的说明和测试结果。

（注意将题目和代码文件保存在当前目录的同一个子目录下）