#!/usr/bin/env python3
"""验证 _verify_order_in_entrust：用 2026-11-03 真实委托表数据测试。

真实委托表（用户核实）：今天只有一条委托
  10:02:18 159981 卖出 5000 @1.867 全部成交 合同号6341864434

测试用例：
  1. 6100股买入  → 必须判定失败并打印原因（委托表无此买入）
  2. 5000股卖出  → 必须判定成功并给出合同号6341864434
  3. getEntrust 返回 unknown err → 必须判定失败（未知），不得当作成功
"""
import sys, os, time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, REPO_DIR)

from trade import _verify_order_in_entrust

# ─── 模拟 EvolvingSim ────────────────────────────────────────

class FakeEntrustError(Exception):
    pass

class FakeE:
    """模拟 EvolvingSim，可控 getEntrust 返回。"""
    def __init__(self, mode='normal'):
        self.mode = mode  # 'normal' / 'error' / 'empty'
    def getEntrust(self, *args):
        if self.mode == 'error':
            # 模拟同花顺 unknown err
            return {'status': False, 'data': None, 'err': 'unknown err'}
        if self.mode == 'empty':
            return {'status': True, 'data': []}
        # normal: 返回真实委托表数据
        # row: [0]日期 [1]时间 [2]代码 [3]名称 [4]操作 [5]备注/状态
        #      [6]委托量 [7]撤销量 [8]委托价 [9]成交价 [10]合同编号
        return {
            'status': True,
            'data': [
                ['20261103', '10:02:18', '159981', '中证1000ETF',
                 '卖出', '全部成交', '5000', '0', '1.867', '1.867', '6341864434'],
            ]
        }

# ─── 测试 ────────────────────────────────────────────────────

def test_1_buy_6100_should_fail():
    """用例1: 6100股买入 → 必须判定失败"""
    print("=" * 60)
    print("【用例1】6100股 买入 → 应判定失败")
    print("=" * 60)
    e = FakeE('normal')
    ok, contract, detail = _verify_order_in_entrust(e, '159981', '买入', 6100)
    if ok:
        print(f"  ❌ 测试失败: 预期失败但返回成功 contract={contract}")
        return False
    print(f"  核验结果: ok={ok}, contract='{contract}'")
    print(f"  判定依据:\n{detail}")
    print(f"  ✅ 测试通过: 6100股买入正确判定为失败")
    return True

def test_2_sell_5000_should_pass():
    """用例2: 5000股卖出 → 必须判定成功并给出合同号6341864434"""
    print("\n" + "=" * 60)
    print("【用例2】5000股 卖出 → 应判定成功，合同号=6341864434")
    print("=" * 60)
    e = FakeE('normal')
    ok, contract, detail = _verify_order_in_entrust(e, '159981', '卖出', 5000)
    if not ok:
        print(f"  ❌ 测试失败: 预期成功但返回失败 detail={detail}")
        return False
    print(f"  核验结果: ok={ok}, contract='{contract}'")
    print(f"  判定依据: {detail}")
    if contract != '6341864434':
        print(f"  ❌ 测试失败: 合同号不匹配，预期6341864434，实际{contract}")
        return False
    print(f"  ✅ 测试通过: 5000股卖出判定成功，合同号=6341864434")
    return True

def test_3_entrust_error_should_fail():
    """用例3: getEntrust 返回异常(unknown err) → 必须判定失败，不得当作成功"""
    print("\n" + "=" * 60)
    print("【用例3】getEntrust 返回 unknown err → 应判定失败(未知)")
    print("=" * 60)
    e = FakeE('error')
    ok, contract, detail = _verify_order_in_entrust(e, '159981', '买入', 6100)
    if ok:
        print(f"  ❌ 测试失败: 接口异常却返回成功")
        return False
    print(f"  核验结果: ok={ok}, contract='{contract}'")
    print(f"  判定依据: {detail}")
    print(f"  ✅ 测试通过: 接口异常正确判定为失败(未知)，未当作成功")
    return True

def test_4_entrust_exception_should_fail():
    """用例4: getEntrust 抛异常 → 必须判定失败"""
    print("\n" + "=" * 60)
    print("【用例4】getEntrust 抛异常 → 应判定失败")
    print("=" * 60)
    class FakeEException:
        def getEntrust(self, *args):
            raise Exception("connection timeout")
    e = FakeEException()
    ok, contract, detail = _verify_order_in_entrust(e, '159981', '卖出', 5000)
    if ok:
        print(f"  ❌ 测试失败: 异常却返回成功")
        return False
    print(f"  核验结果: ok={ok}, contract='{contract}'")
    print(f"  判定依据: {detail}")
    print(f"  ✅ 测试通过: 异常正确判定为失败")
    return True


if __name__ == '__main__':
    results = []
    results.append(test_1_buy_6100_should_fail())
    results.append(test_2_sell_5000_should_pass())
    results.append(test_3_entrust_error_should_fail())
    results.append(test_4_entrust_exception_should_fail())

    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"验证结果: {passed}/{total} 通过")
    print("=" * 60)
    if passed != total:
        sys.exit(1)
    sys.exit(0)
