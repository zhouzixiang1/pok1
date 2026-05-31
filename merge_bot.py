#!/usr/bin/env python3
"""
merge_bot.py - 将多文件 Python bot 合并为单个可运行文件。

用法:
    # 合并单个 bot 目录
    python merge_bot.py bots/claude_v3
    python merge_bot.py bots/claude_v3 /path/to/output.py

    # 扫描 bots/ 下所有子目录，批量合并
    python merge_bot.py --all bots/
    python merge_bot.py -a bots/

    # 智能模式：传入目录自身无 main.py 但子目录有 main.py 时自动批量
    python merge_bot.py bots/
"""

import argparse
import os
import re
import sys
import traceback
from collections import defaultdict, deque


# 匹配 import 语句的正则（单行形式）
# 支持: import x, from x import y, from x import y as z
IMPORT_RE = re.compile(
    r'^\s*(?:from\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+import\b|import\s+([a-zA-Z_][a-zA-Z0-9_]*))'
)


# 默认排除的文件名模式（正则）
DEFAULT_EXCLUDE_PATTERNS = [
    r'.*_backup\.py$',
    r'test_.*\.py$',
    r'.*_test\.py$',
]


def get_module_name(filepath):
    """从文件路径获取模块名（不含扩展名）"""
    return os.path.splitext(os.path.basename(filepath))[0]


def find_py_modules(directory, exclude_patterns=None):
    """查找目录下所有 .py 文件（排除 __pycache__、隐藏文件、备份/测试文件等）"""
    if exclude_patterns is None:
        exclude_patterns = DEFAULT_EXCLUDE_PATTERNS
    modules = {}
    for entry in os.listdir(directory):
        if entry.startswith('_') or entry.startswith('.'):
            continue
        if entry == '__pycache__':
            continue
        if any(re.match(p, entry) for p in exclude_patterns):
            continue
        full = os.path.join(directory, entry)
        if os.path.isfile(full) and entry.endswith('.py'):
            name = get_module_name(full)
            modules[name] = full
    return modules


def parse_file(filepath, local_modules):
    """
    解析文件，提取 import 并分类（支持多行括号形式的 import）。
    返回:
        std_imports: 标准库/第三方 import 语句列表（去重后放到顶部）
        local_from_imports: dict[module, list[names]]  (from X import a, b)
        local_import_modules: list[module]  (import X)
        body_lines: 去掉本地 import 后的代码行（保留换行符）
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    std_imports = []
    local_from_imports = defaultdict(list)
    local_import_modules = []
    body_lines = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            body_lines.append(line)
            i += 1
            continue

        match = IMPORT_RE.match(stripped)
        if match:
            module = match.group(1) or match.group(2)
            is_local = module in local_modules

            # 检测是否是多行括号形式的 import
            block = [line]
            paren_depth = line.count('(') - line.count(')')
            while paren_depth > 0 and i + 1 < len(lines):
                i += 1
                next_line = lines[i]
                block.append(next_line)
                paren_depth += next_line.count('(') - next_line.count(')')

            if match.group(1):  # from X import ...
                if is_local:
                    # 提取导入的名字列表（简单解析）
                    block_text = ''.join(block)
                    rest = block_text.split(' import ', 1)[1]
                    # 去掉括号，按逗号分割
                    rest = rest.replace('(', '').replace(')', '').replace('\n', ' ')
                    names = [n.strip() for n in rest.split(',') if n.strip()]
                    local_from_imports[module].extend(names)
                else:
                    std_imports.extend(block)
            else:  # import X
                if is_local:
                    local_import_modules.append(module)
                else:
                    std_imports.extend(block)

            i += 1
            continue

        body_lines.append(line)
        i += 1

    return std_imports, local_from_imports, local_import_modules, body_lines


def build_dependency_graph(modules):
    """构建模块依赖图，返回 {module_name: set(dependencies)}"""
    graph = defaultdict(set)
    for name, filepath in modules.items():
        _, local_from, local_imp, _ = parse_file(filepath, modules)
        for dep in local_from:
            if dep in modules and dep != name:
                graph[name].add(dep)
        for dep in local_imp:
            if dep in modules and dep != name:
                graph[name].add(dep)
    return graph


def topological_sort(graph, modules):
    """基于 Kahn 算法进行拓扑排序。

    graph[name] = name 依赖的模块集合。
    边方向：被依赖模块 -> 依赖模块（被依赖模块必须先输出）。
    因此模块 m 的入度 = len(graph[m])，即 m 依赖多少其他模块。
    """
    in_degree = {m: len(graph.get(m, set())) for m in modules}

    # 收集反向邻接表：谁依赖 current
    dependents = defaultdict(set)
    for m, deps in graph.items():
        for d in deps:
            dependents[d].add(m)

    queue = deque([m for m, d in in_degree.items() if d == 0])
    result = []

    while queue:
        current = queue.popleft()
        result.append(current)
        for m in dependents.get(current, set()):
            in_degree[m] -= 1
            if in_degree[m] == 0:
                queue.append(m)

    if len(result) != len(modules):
        # 存在循环依赖，将剩余模块按名字排序追加
        remaining = sorted([m for m in modules if m not in result])
        result.extend(remaining)

    return result


def remove_sys_path_insert(lines):
    """去掉 sys.path.insert / sys.path.append 及紧邻的注释行"""
    result = []
    skip_comment = False
    for line in lines:
        stripped = line.strip()
        if 'sys.path.insert' in stripped or 'sys.path.append' in stripped:
            skip_comment = True
            continue
        if skip_comment and stripped.startswith('#'):
            if 'local modules' in stripped.lower() or 'importable' in stripped.lower():
                continue
            skip_comment = False
        result.append(line)
    return result


def strip_trailing_blank_lines(lines):
    """去掉末尾的空白行"""
    while lines and lines[-1].strip() == '':
        lines.pop()
    return lines


def dedup_imports(std_imports):
    """去重并排序标准库 import"""
    seen = set()
    result = []
    for imp in std_imports:
        key = imp.strip()
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def merge_single_bot(source_dir, output_file=None):
    """合并单个 bot 目录。"""
    source_dir = os.path.abspath(source_dir)
    if output_file is None:
        output_file = source_dir.rstrip('/') + '_merged.py'
    else:
        output_file = os.path.abspath(output_file)

    if not os.path.isdir(source_dir):
        raise ValueError(f"{source_dir} 不是有效目录")

    modules = find_py_modules(source_dir)
    if not modules:
        raise ValueError(f"在 {source_dir} 中未找到 .py 文件")

    # 单文件 bot（只有 main.py）：直接复制
    if len(modules) == 1 and 'main' in modules:
        with open(modules['main'], 'r', encoding='utf-8') as f:
            lines = f.readlines()
        lines = remove_sys_path_insert(lines)
        lines = strip_trailing_blank_lines(lines)

        header = f'''# Merged bot from: {source_dir}
# Generated by merge_bot.py
# Single-file bot (no modules to merge)

'''
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(header)
            f.write(''.join(lines))
            f.write('\n')
        return output_file, 1

    # 多文件 bot：拓扑排序合并
    graph = build_dependency_graph(modules)
    order = topological_sort(graph, modules)

    all_std_imports = []
    merged_sections = []
    proxy_modules = set()

    for mod_name in order:
        filepath = modules[mod_name]
        std_imp, local_from, local_imp, body = parse_file(filepath, modules)
        all_std_imports.extend(std_imp)
        proxy_modules.update(local_imp)

        body = remove_sys_path_insert(body)
        body = strip_trailing_blank_lines(body)

        section_lines = [f"\n# {'='*60}\n# Module: {mod_name}\n# {'='*60}\n\n"]
        section_lines.extend(body)
        merged_sections.append(''.join(section_lines))

    # 如果有 import X 形式的本地模块引用，插入模块代理类
    if proxy_modules:
        proxy_code = '''\n# ============================================================
# Module proxies for "import X" style references
# ============================================================\n
class _ModuleProxy:
    """模拟模块命名空间，使 import X 后的 X.attr 访问指向全局命名空间。"""
    def __init__(self, name):
        self._name = name
    def __getattr__(self, name):
        return globals()[name]
    def __setattr__(self, name, value):
        if name.startswith('_'):
            super().__setattr__(name, value)
        else:
            globals()[name] = value

'''
        for pm in sorted(proxy_modules):
            proxy_code += f'{pm} = _ModuleProxy("{pm}")\n'
        proxy_code += '\n'
        merged_sections.insert(0, proxy_code)

    std_import_section = '\n'.join(dedup_imports(all_std_imports))

    header = f'''# Merged bot from: {source_dir}
# Generated by merge_bot.py
# Modules merged ({len(order)}): {', '.join(order)}

'''

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(header)
        if std_import_section:
            f.write(std_import_section)
            f.write('\n')
        for section in merged_sections:
            f.write(section)
            f.write('\n')

    return output_file, len(order)


def is_batch_mode(source_dir, force_all=False):
    """判断是否应进入批量模式。

    启发式规则：
    - 显式 --all 标志 => 批量
    - 传入目录自身包含 main.py => 单目录
    - 传入目录的子目录中至少有一个包含 main.py => 批量
    - 否则 => 单目录
    """
    if force_all:
        return True

    # 自身包含 main.py => 单目录模式
    if os.path.isfile(os.path.join(source_dir, 'main.py')):
        return False

    # 子目录中有包含 main.py 的 => 批量模式
    if os.path.isdir(source_dir):
        for entry in os.listdir(source_dir):
            sub = os.path.join(source_dir, entry)
            if os.path.isdir(sub) and not entry.startswith('.') and not entry.startswith('_'):
                if os.path.isfile(os.path.join(sub, 'main.py')):
                    return True

    return False


def merge_all_bots(source_dir):
    """批量扫描 source_dir 下的所有 bot 子目录并合并。"""
    source_dir = os.path.abspath(source_dir)
    if not os.path.isdir(source_dir):
        print(f"错误: {source_dir} 不是有效目录")
        sys.exit(1)

    bot_dirs = []
    for entry in sorted(os.listdir(source_dir)):
        sub = os.path.join(source_dir, entry)
        if not os.path.isdir(sub) or entry.startswith('.') or entry.startswith('_'):
            continue
        bot_dirs.append(sub)

    if not bot_dirs:
        print(f"错误: 在 {source_dir} 中未找到子目录")
        sys.exit(1)

    success = 0
    skipped = 0
    failed = 0

    print(f"开始批量合并，共 {len(bot_dirs)} 个目录...\n")

    for bot_dir in bot_dirs:
        bot_name = os.path.basename(bot_dir)
        output_file = bot_dir.rstrip('/') + '_merged.py'

        modules = find_py_modules(bot_dir)
        if not modules:
            print(f"[跳过] {bot_name}: 无 .py 文件")
            skipped += 1
            continue

        try:
            out_path, mod_count = merge_single_bot(bot_dir, output_file)
            if mod_count == 1:
                print(f"[成功] {bot_name}: 单文件直通 → {os.path.basename(out_path)}")
            else:
                print(f"[成功] {bot_name}: {mod_count} 个模块合并 → {os.path.basename(out_path)}")
            success += 1
        except Exception as e:
            print(f"[失败] {bot_name}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"批量合并完成: 成功 {success} 个, 跳过 {skipped} 个, 失败 {failed} 个")
    return success, skipped, failed


def main():
    parser = argparse.ArgumentParser(
        description='将多文件 Python bot 合并为单个可运行文件。'
    )
    parser.add_argument('source', help='源目录（单 bot 目录或 bots 父目录）')
    parser.add_argument('output', nargs='?', help='输出文件路径（仅单目录模式有效）')
    parser.add_argument('-a', '--all', action='store_true', help='强制批量模式：扫描 source 下所有子目录')
    args = parser.parse_args()

    source_dir = os.path.abspath(args.source)

    if not os.path.exists(source_dir):
        print(f"错误: 路径不存在: {source_dir}")
        sys.exit(1)

    # 判断模式
    batch = is_batch_mode(source_dir, force_all=args.all)

    if batch:
        if args.output:
            print("警告: --all 模式下 output 参数被忽略")
        merge_all_bots(source_dir)
    else:
        try:
            out_path, mod_count = merge_single_bot(source_dir, args.output)
            if mod_count == 1:
                print(f"\n单文件直通完成: {out_path}")
            else:
                print(f"\n合并完成: {out_path}")
                print(f"总模块数: {mod_count}")
        except Exception as e:
            print(f"错误: {e}")
            sys.exit(1)


if __name__ == '__main__':
    main()
