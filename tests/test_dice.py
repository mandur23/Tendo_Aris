"""주사위 표기(NdM+K) 파서·판정 테스트."""
import pytest

from GameSystem.TRPGEngine import CheckResult, TRPGCharacter, roll_check, roll_dice


def test_기본_표기_파싱():
    r = roll_dice("2d6+3")
    assert len(r.rolls) == 2
    assert all(1 <= x <= 6 for x in r.rolls)
    assert r.modifier == 3
    assert r.total == sum(r.rolls) + 3


def test_개수_생략과_대문자와_음수보정():
    r = roll_dice("d20")
    assert len(r.rolls) == 1 and 1 <= r.rolls[0] <= 20 and r.modifier == 0

    r = roll_dice("3D8-2")
    assert len(r.rolls) == 3 and r.modifier == -2
    assert r.notation == "3d8-2"


@pytest.mark.parametrize("bad", ["", "abc", "0d6", "21d6", "2d1", "2d9999", "d"])
def test_잘못된_표기는_거부(bad):
    with pytest.raises(ValueError):
        roll_dice(bad)


def test_판정_자연20은_대성공_자연1은_대실패():
    char = TRPGCharacter.create("용사", "warrior")
    crit = CheckResult(stat="힘", roll=20, mod=-5, dc=30)
    assert crit.success and crit.band == "대성공"
    fumble = CheckResult(stat="힘", roll=1, mod=99, dc=5)
    assert not fumble.success and fumble.band == "대실패"

    check = roll_check(char, "힘", dc=12)
    assert 1 <= check.roll <= 20
    assert check.mod == char.stats["힘"]
