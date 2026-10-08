"""Fill empty GBVSR glossary translations without changing reviewed entries.

The glossary is deliberately kept as the human-editable source of truth.  This
tool only handles recurring Dustloop phrasing and reports phrases that need a
manual pass instead of overwriting any existing translation.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


GLOSSARY_PATH = Path("data/gbvsr/notes/_glossary.json")

# Dustloop sometimes emits isolated parser remnants as notes.  They have no
# displayable meaning and must not become a Chinese note in downstream files.
NON_NOTES = {"2.", ": 4, 3"}

# Character names stay in Dustloop's English form, matching docs/gbf.md.
NAME_FIXES = {
    "贝阿朵莉切": "Beatrix",
    "贝阿朵丽丝": "Beatrix",
    "贝利尔布": "Beelzebub",
    "贝利亚尔": "Belial",
    "卡塔莉娜": "Katalina",
    "卡塔琳娜": "Katalina",
    "卡利奥斯特罗": "Cagliostro",
    "圣德芬": "Sandalphon",
    "齐格飞": "Siegfried",
    "兰斯洛特": "Lancelot",
    "梅忒拉": "Metera",
    "梅塔拉": "Metera",
    "菲莉": "Ferry",
    "伽利略": "Galleon",
    "伽勒翁": "Galleon",
    "洛艾因": "Lowain",
    "伊尔莎": "Ilsa",
    "艾尔莎": "Ilsa",
    "希斯": "Seox",
    "索利兹": "Soriz",
    "姬塔": "Djeeta",
    "玛丽": "Mari",
}

KNOWN_CORRECTIONS = {
    "2.": "",
    ": 4, 3": "",
    "Data in {} denotes on Catch on whiff.": "{}括号内为挡住攻击后挥空时的数据。",
    "Data in [] denotes after Catch.": "[]括号内为挡住攻击后的数据。",
    "Data in [] denotes on Catch.": "[]括号内为挡住攻击时的数据。",
    "Data assumes input was performed IAS Instant Air Special Conceptually includes Tiger Knee. Performing a special as soon as possible after becoming airborne. Usually, but not always, involves an input trick. For Example: 2369S for a j.236S input. .": "以上数据为最速出招数据，角色一离地就出招（低空必杀技）。",
    "Advantages assume input was done IAS Instant Air Special Conceptually includes Tiger Knee. Performing a special as soon as possible after becoming airborne. Usually, but not always, involves an input trick. For Example: 2369S for a j.236S input. .": "以上数据为最速出招数据，角色一离地就出招（低空必杀技）。",
}


MANUAL_TRANSLATIONS = {
    "Consumes all remaining SKL and reduces damage by 50% if used with less than that.": "消耗剩余所有 SKL；若剩余不足，伤害降低 50%。",
    "Cagliostro is air actionable after the teleport.": "传送后可在空中行动。",
    "Recovers 30 Dragon Gauge starting frame 12 for 50 frames, to a total of 1500 units (750 if Lyrn is active). On hit, recovery extends (to 102F Total), and recovers an additional 1500 units (750 if Lyrn is active)": "从第12帧开始，恢复30点龙之能量，持续50帧，总计恢复1500点（若Lyrn处于激活状态，则恢复750点）。命中后，恢复时间延长（总计恢复102帧），并额外恢复1500点（若Lyrn处于激活状态，则恢复750点）。",
    "Damage of Meteorite is 300/500/700/900 for level 1/2/3/4.": "陨石在等级 1/2/3/4 时的伤害为 300/500/700/900。",
    "Combo throw against opponents in hitstun, including aerial opponents.": "可对受击硬直中的对手使用连段投，包括空中的对手。",
    "Raises Blade Level by 0.2 2,000 points .": "每获得 2,000 点提高 0.2 剑等级。",
    "Removes Sandalphon's elemental buff on use.": "使用时移除圣德芬的元素强化。",
    "Pact restrictions only apply if the Pact actually hit the opponent.": "契约的限制仅在契约实际命中对手时生效。",
    "First hit to connect alone is a Low.": "单独命中的第一段为下段。",
    "Data varies based on height of contact, and is listed at point blank range.": "数据会随接触高度变化，表中列出的是贴身距离的数据。",
    "Advantages refer to Front Throw/Back Throw respectively": "优势帧依次对应前投/后投。",
    "Damage increases to 600, 180×15, 60×17, 1440 [3000] when Djeeta is at 30% HP or less.": "姬塔生命值不高于 30% 时，伤害提高为 600、180×15、60×17、1440 [3000]。",
    "Horizontal Pushback: 50 onBlock": "被防御时水平击退距离：50。",
    "Data does not assume a 20% damage increase due to , but it will increase to 120×30, 840 if Bravery Penalty is triggered by the close hit of Tempest Blade. (Tempest Blade takes one BP and Eternal Edge takes a second, leaving the opponent in Bravery Penalty).": "数据未计入 20% 伤害增加；若暴风剑的近距离命中触发勇气惩罚，伤害会变为 120×30、840。（暴风剑消耗 1 点勇气点，永恒之刃再消耗 1 点，使对手进入勇气惩罚。）",
    "Does not combo into itself on normal hit.": "普通命中时不能连到自身。",
    "Data assumes hit properties apply to the initial 214X.": "数据假定初始 214X 的命中属性生效。",
    "Duration includes 5U travel time.": "总时长包含 5U 的移动时间。",
    "Data assumes hit properties apply to the initial 623X.": "数据假定初始 623X 的命中属性生效。",
    "Data assumes hit properties of initial 22X.": "数据假定初始 22X 的命中属性生效。",
    "Data assumes contacting with the falling shots, not the rising shot.": "数据假定由下落攻击命中，而非上升攻击命中。",
    "Data is for the lightning strikes of Thunder.": "数据对应 Thunder 的雷击部分。",
    "First hit gives 60 meter if landed raw, but 40 if used mid-combo. Subsequent hits give 10.": "第一段裸命中获得 60 奥义槽，连段中命中获得 40；后续每段获得 10。",
    "2B cannot cancel the ensuing dash into 4G or 6G .": "2B 不能将后续冲刺取消为 4G 或 6G。",
    "Lasts for 360F 6 seconds or until 2B is hit.": "持续 360 帧（6 秒），或直到 2B 被命中。",
    "First hit always deals 400 damage, regardless of how late it connects.": "第一段无论在何时命中，固定造成 400 伤害。",
    "Cannot be comboed after landing.": "落地后无法继续连段。",
    "First and third sheep travel offscreen, second sheep ends about 75% across after 61F.": "第一、第三只羊会飞出屏幕；第二只羊在 61 帧后停在约四分之三屏处。",
    "Data assumes 623X 6S \u007f'\"`UNIQ--templatestyles-00000108-QINU`\"'\u007f made contact.": "数据假定 623X 6S 接触到对手。",
    "HKD +71 on wallbounce with no follow-up hit.": "无派生命中时，墙弹后为 HKD +71。",
    "Ignores invincibility on Catch.": "抓取时无视无敌。",
    "Bits are not affected by Technical Input Bonus of the install command.": "雷枪不受安装技的技术输入奖励影响。",
    "Combo limit scaling refers to bits.": "连击限制增加值对应雷枪。",
    "Will not trigger Combo Limit.": "不会增加连击限制。",
    "Deals 1500 damage to Beatrix on successful throw.": "成功投中贝阿朵莉切时造成 1500 伤害。",
    "Delta Clock only activated if initial strike hits.": "仅初始打击命中时才会发动 Delta Clock。",
    "Combo Limit Scaling of 2 when only the final hit connects.": "仅最后一段命中时，连击限制增加值为 2。",
    "Combo throw against opponents in grounded hitstun.": "可对地面受击硬直中的对手使用连段投。",
    "Transitions into cinematic finisher if the first hit lands.": "第一段命中时会进入演出终结。",
    "Belial moves for a minimum of 24F before throw becomes available": "贝利亚尔至少移动 24 帧后才能使用投技。",
    "Will perform cinematic if any part of first hitbox touches opponent, regardless of spacing.": "第一段任一判定接触到对手即进入演出，与距离无关。",
    "- Inactive denotes H follow-up only.": "- Inactive 表示仅 H 派生具有该项。",
    "Second hit only occurs if first connects, instantly canceling from first set of actives into inactive and second active.": "仅第一段命中时才会出现第二段；会从第一组持续帧立即取消到非持续状态，再进入第二组持续帧。",
    "Total duration depends on when the fireball makes contact.": "总时长取决于火球何时接触对手。",
    "At far ranges, it is possible to get only the second hit to land.": "在远距离时，可能只有第二段命中。",
    "Deals 800 damage either on ground explosion or on stick, and an additional 800 when activated from a gunfire move.": "地面爆炸或黏附时各造成 800 伤害；由枪击招式触发时额外造成 800 伤害。",
    "[Detonate] is air unblockable.": "[引爆] 在空中不可防御。",
    "Will not combo into an active U Rat Race explosion.": "无法连到正在生效的 U Rat Race 爆炸。",
    "Cancel window: 35- ?? i don't want to brute force look for it... F": "招式取消窗口：35 帧至未知帧。",
    "Cancel window: 51- ?? still don't wanna F": "招式取消窗口：51 帧至未知帧。",
    "Cancel window: 50- ?? not gonna happen F": "招式取消窗口：50 帧至未知帧。",
    "Only first hit makes contact on crouchblocking opponents, leaving Ferry -14 instead of -8.": "对手蹲姿防御时只有第一段接触，菲莉为 -14 而非 -8。",
    "On-Contact data refers to connecting on first active.": "接触数据指在第一帧持续帧命中时的数据。",
    "-33 when crouch blocked": "蹲姿防御时为 -33。",
    "Move is more plus the further out Ferry is from the other player.": "菲莉与对手距离越远，本招命中后的优势帧越大。",
    "Orb will always come out so long as the Superflash plays, even if Ferry is hit out of it.": "只要暗转出现，法球一定会发射，即使菲莉在期间被打断。",
    "20% damage penalty for Ferry while active, except throws, Raging Strikes, Raging Chains, and Brave Counters.": "效果持续期间，菲莉的伤害降低 20%；投技、怒火强攻、怒火突袭和英勇反击除外。",
    "The first hit that makes contact is always a low.": "最先接触到对手的一段固定为下段。",
    "Places Meteorite about two character lengths in front of Galleon.": "在伽利略身前约两个身位处放置陨石。",
    "Damage of Meteorite is 300/500/700/900 base damage, scaled 70% at the start of the combo, for 210/350/490/630 damage for level 1/2/3/4.": "陨石基础伤害为 300/500/700/900；作为连段起手时按 70% 修正，等级 1/2/3/4 分别造成 210/350/490/630 伤害。",
    "Outlasts backdash's throw invulnerability.": "持续时间超过后跳的投无敌时间。",
    "Uses Meteorite level present on screen. If none has been set, sets it to Level 0.": "使用画面上现有陨石的等级；若尚未设置陨石，则设为等级 0。",
    "First part does 3 hits, second part does 3 hits. Both parts can only land a max total of 6 hits combined.": "第一部分 3 段、第二部分 3 段；两部分合计最多命中 6 段。",
    "Second hit only occurs if first connects, instantly canceling from first set of actives into inactive and second active": "仅第一段命中时才会出现第二段；会从第一组持续帧立即取消到非持续状态，再进入第二组持续帧。",
    ": 4, 3": "数据：4、3。",
    "Skybound gauge cooldown stays until the Wind Gauge is exhausted, after that enters 300F SBG cooldown.": "奥义槽冷却会持续到风槽耗尽，之后进入 300 帧的奥义槽冷却。",
    "Data assumes 9 bullets are stocked. See Full Data Table for extra details.": "数据假定已装填 9 发子弹；详情见完整数据表。",
    "Data assumes Ilsa has 2+ bullets and is performing 214X into the corner.": "数据假定伊尔莎有至少 2 发子弹，且 214X 将对手打向版边。",
    "Damage when input as 236S + U : 396×4, 26×100, 2640": "输入 236S+U 时的伤害：396×4、26×100、2640。",
    "Damage when input as 720U : 435×4, 29×100, 2904": "输入 720U 时的伤害：435×4、29×100、2904。",
    "Listed data assumes input 5S + U +Throw.": "表中数据假定输入 5S+U+投。",
    "Due to a rounding error, the 236S + U input only deals 6824 damage total rather than the full 6864 it should.": "由于取整误差，236S+U 实际总伤害为 6824，而非理论上的 6864。",
    "Due to a rounding error, the 720U input deals 7544 damage rather than the intended 7550 it should.": "由于取整误差，720U 实际伤害为 7544，而非理论上的 7550。",
    "First hit to make contact will always be an overhead, all subsequent hits will be mids.": "最先接触的一段固定为中段，之后各段均为无段。",
    "Lancelot is air actionable immediately after the wall jump.": "兰斯洛特墙跳后立即可在空中行动。",
    "22L : If the opponent is 250,000 Distance Units (DU) Most characters have pushbox widths of 125,000 DU. away or closer, teleports Lancelot 65,000 DU forward. If the opponent is between 250,001-300,000 DU away, teleports Lancelot to the opponent and 150,000 DU in front of them. If the opponent is over 300,000 DU away, teleports Lancelot 150,000 DU forward.": "22L：对手距离不超过 250,000 DU 时，兰斯洛特前移 65,000 DU；距离为 250,001-300,000 DU 时，传送至对手前方 150,000 DU；超过 300,000 DU 时，前移 150,000 DU。",
    "22M : If the opponent is 450,000 DU away or closer, teleports Lancelot to the opponent and 200,000 DU in front of them. If the opponent is over 450,000 DU away, teleports Lancelot 300,000 DU forward.": "22M：对手距离不超过 450,000 DU 时，传送至对手前方 200,000 DU；超过 450,000 DU 时，前移 300,000 DU。",
    "22H : If the opponent is 900,000 DU away or closer, teleports Lancelot to the opponent and 200,000 DU in front of them. If the opponent is over 900,000 DU away, teleports Lancelot 900,000 DU forward.": "22H：对手距离不超过 900,000 DU 时，传送至对手前方 200,000 DU；超过 900,000 DU 时，前移 900,000 DU。",
    "22[H] ground bounces opponent higher on air hit when landing as the first hit only.": "22[H] 作为第一段在空中命中并使对手落地时，会造成更高的地面弹地。",
    "Debuff ends early if Lancelot blocks, is hit, or if the opponent uses any Skybound Art.": "兰斯洛特防御、被命中，或对手使用任意奥义时，减益会提前结束。",
    "First hit Clash Level 18. Second hit Clash Level 10.": "第一段相杀等级为 18，第二段相杀等级为 10。",
    "Second hit tracks opponent up to 550,000 Distance Units (DU) Most characters have pushbox widths of 125,000 DU. in front of Lancelot, and teleports him 550,000 DU forward if the opponent is farther than that.": "第二段会追踪兰斯洛特前方 550,000 DU 内的对手；若对手更远，则兰斯洛特前移 550,000 DU。",
    "On-Block varies based on range": "被防御后的帧数随距离变化。",
    "Combo limiting scaling is 2 regardless of only one or both hitting.": "无论仅一段还是两段命中，连击限制增加值均为 2。",
    "Max damage: 3260": "最大伤害：3260。",
    "The Katalina Bot has a pushbox that extends vertically if Lowain jumps, preventing any combo extension.": "洛艾因跳跃时，卡塔莉娜机器人会拥有向上延伸的碰撞箱，导致无法继续连段。",
    "Install duration: 420F": "安装状态持续 420 帧。",
    "Any hit may cancel into 236236H ~ 236U .": "任意一段命中都可取消为 236236H~236U。",
    "Damage is independent of HPA activation method and will still get the Technical bonus when performed as 236U .": "伤害不受 HPA 发动方式影响；以 236U 发动时仍可获得技术输入奖励。",
    "Minimum damage: 2000": "最低伤害：2000。",
    "KDs on normal hit, HKD on counter hit.": "普通命中为 KD，康特命中为 HKD。",
    "Mari cannot be called again for 63F after disappearing normally, or for 114F if she was hit by an attack instead.": "玛丽正常消失后 63 帧内无法再次召唤；若被攻击打中而消失，则为 114 帧。",
    "Combo launch minimum 13F when 623H combos only.": "仅由 623H 连段时，连段最低浮空时间为 13 帧。",
    "Can press 4U to retreat instead.": "可改按 4U 后撤。",
    "Can press j.4U to retreat instead.": "可改按 j.4U 后撤。",
    "Metera can only use one Zephyr per jump, though she may perform any air normal or special instead.": "梅忒拉每次跳跃只能使用一次 Zephyr；仍可使用任意空中普通技或必杀技。",
    "Detonate can be triggered by any of Metera's arrow moves: f.H , 2H / 1H , 236X, 214X, j.236X, j.214X, 623X, and 236236U .": "梅忒拉的任意箭矢招式均可触发引爆：f.H、2H/1H、236X、214X、j.236X、j.214X、623X、236236U。",
    "H Version: Both explosions count as separate hits for scaling.": "H 版本：两次爆炸均作为独立攻击计入伤害修正。",
    "Butterfly lasts for 240F if not detonated.": "蝴蝶若未引爆，会持续 240 帧。",
    "Far version wall bounces in the corner.": "远距离版本在版边会造成墙弹。",
    "Can immediately go into block or dash after animation ends.": "动画结束后可立即防御或冲刺。",
    "During cross-up animation: 1-13 Passthrough No collision box": "换边动画期间：1-13 帧可穿过对手，且没有碰撞箱。",
    "Travels a set distance. This move is collision based, there is no traditional hitbox.": "移动固定距离。本招基于碰撞判定，没有传统的攻击判定框。",
    "Will sideswap even in the corner.": "即使在版边也会换边。",
    "Notably the same damage as the M version": "伤害与 M 版本相同。",
    "Only does the first two swings when whiffed.": "挥空时仅会使出前两次挥击。",
    "From 13 to 9 Hearts: 3800 (2600, 100x12) • From 8 to 4 Hearts: 4500 (3300, 100x12) • From 3 to 1 Hearts: 5200 (4000, 100x12)": "13-9 颗心：3800（2600、100×12）；8-4 颗心：4500（3300、100×12）；3-1 颗心：5200（4000、100×12）。",
    "Brackets denotes the situation when it is activated raw • Only triggers strike when canceled from normals and opponent is in hitstun • When on 9-13 Hearts, damage is 1440×2, 1680 • When on 4-8 Hearts, damage is 720×6, 1080 • When on 1-3 Hearts, damage is 420×12, 1200": "括号内为裸发动时的数据。仅由普通技取消且对手处于受击硬直时才会触发打击。13-9 颗心时伤害为 1440×2、1680；4-8 颗心时为 720×6、1080；1-3 颗心时为 420×12、1200。",
    "Leaves a flame carpet on the ground, dealing damage over time for a short period of time if opponent is standing or crouching over it (maximum 1670 damage over 2 seconds)": "在地面留下火焰地毯；对手站立或蹲在其上时会短暂持续受伤（2 秒内最多 1670 伤害）。",
    "Data listed assumes 0 Orbs were pre-stocked. See Full Data Table for extra details.": "数据假定预先储存的法球为 0；详情见完整数据表。",
    "Combo launch minimum 6F when used with 5 Orbs, increases by 6F per Orb already stocked to a maximum of 30F when used at 9 Orbs.": "使用时有 5 个法球则连段最低浮空时间为 6 帧；每多预存 1 个法球增加 6 帧，9 个法球时最高为 30 帧。",
    "Combo launch wallbounce minimum 7F.": "墙弹连段最低浮空时间为 7 帧。",
    "Combo launch wallbounce minimum 12F.": "墙弹连段最低浮空时间为 12 帧。",
    "7F follow up window on counter hit only.": "仅康特命中时有 7 帧派生窗口。",
    "Install lasts 480F.": "安装状态持续 480 帧。",
    "Seox's attacks deal only 60% of their usual damage while install is active.": "安装状态持续期间，希斯的攻击仅造成通常伤害的 60%。",
    "Combo launch 5F on normal hit.": "普通命中时连段浮空时间为 5 帧。",
    "Consumes half of Siegfried's current health and refunds Skybound Gauge equal to the percentage of health consumed.": "消耗齐格飞当前生命值的一半，并回复等同于消耗生命百分比的奥义槽。",
    "For the rest of the round, increases Siegfried's initial dash speed by 26.7% from 10.8 to 13.7 , dash acceleration by 16.6% from 0.540 to 0.630 , forward jump distance by 11.1% from 9000 to 10000 , and high jump distance by 34.6% from 13000 to 17500 . Decreases his jump height by 8.3% from 42000 to 38500 and high jump height by 9.8% from 51000 to 46000 .": "本回合剩余时间内：齐格飞初始冲刺速度由 10.8 提高至 13.7（+26.7%），冲刺加速度由 0.540 提高至 0.630（+16.6%），前跳距离由 9000 提高至 10000（+11.1%），大跳距离由 13000 提高至 17500（+34.6%）；跳跃高度由 42000 降至 38500（-8.3%），大跳高度由 51000 降至 46000（-9.8%）。",
    "Armor loses to , , and .": "霸体会被特定攻击克制。",
    "Forces a knockdown on air hit that is almost always unable to be picked up in a combo.": "空中命中时强制击倒，几乎无法再继续连段。",
    "Second hit will wait until the opponent falls low enough for it to land if the first hit lands too high as an anti-air.": "若第一段作为对空命中得过高，第二段会等待对手下降到足够低的位置再命中。",
    "Final hit in mu.214U deals 1200 damage if the opponent blocks the rest of it.": "mu.214U 的最后一段在其余部分被防御时造成 1200 伤害。",
    "H version wall bounces in the corner on normal hit.": "H 版本普通命中时在版边会造成墙弹。",
    "Requires 100% Skybound Gauge to use, but does not spend any.": "需要 100% 奥义槽才能使用，但不会消耗奥义槽。",
    "Soriz recovers 300 HP for each Manliness stack he had during the superflash. He cannot heal above 30% of his max HP.": "暗转时索利兹每层男子气概回复 300 HP，且无法回复至最大生命值的 30% 以上。",
    "Increases Soriz's defense by +30/40/50/60/100% when he has 1/2/3/4/5 Manliness stacks respectively while install is active.": "安装状态期间，索利兹拥有 1/2/3/4/5 层男子气概时，防御力分别提高 30/40/50/60/100%。",
    "Increases Soriz's dash acceleration from 0.42 to 0.57 while install is active.": "安装状态期间，索利兹的冲刺加速度由 0.42 提高至 0.57。",
    "Goes into cinematic if either stomp or punch land.": "践踏或拳击任一命中时会进入演出。",
    "Stomp is not comboable.": "践踏不能用于连段。",
    "Damage cap: 15999.": "伤害上限：15999。",
    "Attack comes out 6F after releasing on charged versions.": "蓄力版本松开按键后 6 帧发动攻击。",
    "Attack comes out 7F after releasing on charged versions.": "蓄力版本松开按键后 7 帧发动攻击。",
    "Can be held to manually delay attack, or is delayed by taking hitstun.": "可按住手动延后攻击；受到受击硬直时也会延后攻击。",
    "Restarts Savage Rampage stance timer after use.": "使用后重置 Savage Rampage 架势的持续计时。",
    "Can be input with or G .": "也可使用 G 输入。",
    "Superarmor takes 0% damage but will inflict an uncancellable hitstop state to the opponent if connected with.": "强霸体承受攻击时受到 0% 伤害；若攻击接触到对手，会使对手进入无法取消的命中停顿。",
    "Consumes Celestial Dominion buff": "消耗『他化自在』强化。",
    "Cooldown starts 3F after projectile has disappeared on all versions": "各版本均在飞行道具消失后第3帧开始冷却。",
    "Cooldown starts 3F after the projectile disappears": "飞行道具消失后第3帧开始冷却。",
    "Tracks opponent's location frames 1-6, projectile appears 16 frames afterwards": "第1-6帧追踪对手位置，16帧后出现飞行道具。",
    "Projectile Clash Level 2": "飞行道具相杀等级为2。",
    "236H ~ U both causes combo launch for 13F and increases HKD advantage to +67 on hit only.": "236H~U 两段均可造成13帧连段浮空，且仅命中时HKD优势帧增加至+67。",
    "50% chip damage": "50% 削血伤害。",
    "Restores health when picked up by either character.": "任一角色拾取后回复生命值。",
    "Gifts 10% Skybound Gauge when picked up by either character.": "任一角色拾取后获得 10% 奥义槽。",
    "Damage in brackets refers to non-cinematic hit.": "括号内伤害对应不进入演出时的命中。",
    "13F minimum Combo Launch on Normal Hit.": "普通命中时连段最低浮空时间为 13 帧。",
    "KD +18 when comboed from Normal Hit lu.5U .": "由普通命中 lu.5U 连段时为 KD +18。",
    "Knocks down on air hit.": "空中命中时击倒。",
    "1F gap between fourth and fifth hit.": "第四段与第五段之间有 1 帧空隙。",
    "Minimum 8F combo launch on wallbounce": "墙弹时连段最低浮空时间为 8 帧。",
    "6F combo launch": "连段浮空时间为 6 帧。",
    "Move can only hit once despite two disjointed active intervals.": "尽管有两段分离的持续帧，本招仅能命中一次。",
    "-65 if dodged twice after the superflash.": "暗转后被两次闪避时为 -65。",
    "First hit will always be an Overhead, even if hitting during the second active phase.": "第一段固定为中段，即使在第二段持续帧命中也是如此。",
    "Move must hit a minimum of twice.": "本招至少会命中两次。",
    "Data includes 214H travel time.": "数据包含 214H 的移动时间。",
    "2nd and 3rd hits will launch grounded opponents.": "第二、第三段会使地面上的对手浮空。",
    "Same data applies for 1X, 2X, and 3X follow-ups. Will not trigger Combo Limit.": "相同数据适用于 1X、2X、3X 派生，且不会增加连击限制。",
    "Same data applies for 4X, 5S , and 6X follow-ups. Will not trigger Combo Limit.": "相同数据适用于 4X、5S、6X 派生，且不会增加连击限制。",
    "Same data applies for 7X, 8X, and 9X follow-ups. Will not trigger Combo Limit.": "相同数据适用于 7X、8X、9X 派生，且不会增加连击限制。",
    "3 +18 if only the final lightning strike CHs": "仅最后一发雷击康特命中时为 +18。",
    "4 +44 in the corner": "版边时为 +44。",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _keep_english_names(text: str) -> str:
    for chinese, english in NAME_FIXES.items():
        text = text.replace(chinese, english)
    return text


def _translate_data_clause(clause: str) -> str:
    clause = _clean(clause).rstrip(".")
    replacements = {
        "on Catch on whiff": "挡住攻击后挥空时",
        "on whiff": "挥空时",
        "denotes on whiff": "挥空时",
        "on contact": "接触到对手时",
        "during on contact": "接触到对手时",
        "on hit": "命中时",
        "on block": "被防御时",
        "on Catch": "挡住攻击时",
        "after Catch": "挡住攻击后",
        "far version": "远距离版本",
        "H version": "H版本",
        "charged version": "蓄力版本",
        "maximum charge": "最大蓄力时",
        "non-cinematic far hit": "远距离命中且不进入演出时",
        "non-cinematic far version": "远距离版本且不进入演出时",
        "non-cinematic hit": "不进入演出时的命中",
        "non-cinematic hit": "不进入演出时的命中",
        "when no follow-up is executed": "未使出派生招式时",
        "when using an orb": "使用法球时",
        "when triggering Meteorite": "触发陨石时",
        "when activating Work the Crowd": "发动 Work the Crowd 时",
        "when activating": "发动时",
        "when triggering": "触发时",
        "when not held": "未按住时",
        "on Detonate": "引爆时",
        "during Luminiera form": "光之剑形态期间",
        "during Six-Ruin's Enlightenment": "六崩之悟状态期间",
        "when opponent has no Bravery Points left": "对手勇气点数耗尽时",
        "when countering a Raging Strike": "反制怒火强攻时",
        "if first hit whiffs": "第一段挥空时",
        "if the first hit whiffs": "第一段挥空时",
        "when the first hit whiffs": "第一段挥空时",
        "when only the second hit lands": "仅第二段命中时",
        "when only second hit lands": "仅第二段命中时",
        "when only the final hit lands": "仅最后一段命中时",
        "when rising hitbox whiffs": "上升判定挥空时",
        "uncharged Thunder strike": "未蓄力的雷击时",
        "the projectile portion": "飞行道具部分",
        "the startup when canceled from j.U": "由 j.U 招式取消后的启动帧",
        "when cancelled from a successful 5U / 6U": "成功命中 5U / 6U 后取消时",
        "when cancelled from a successful j.5U": "成功命中 j.5U 后取消时",
        "when canceled into from an H skill": "由 H 必杀技取消而来时",
        "when comboing from 214M": "由 214M 连段而来时",
        "when comboing from 214H": "由 214H 连段而来时",
        "when comboing from 236X": "由 236X 连段而来时",
        "when 623U is triggered": "触发 623U 时",
        "when 623H is triggered": "触发 623H 时",
        "when using a Träumerei Orb": "使用 Träumerei 法球时",
        "when a bit fires from Seven Spears of Lightning": "七雷枪的雷枪发射时",
        "when using an orb": "使用法球时",
        "when used mid-combo": "连段中使用时",
        "when hitting outside the first three active frames": "未在最初 3 帧持续帧内命中时",
        "when missing first two active frames (Clean Hit) window": "错过最初 2 帧持续帧（净命中窗口）时",
    }
    for english, chinese in sorted(replacements.items(), key=lambda item: -len(item[0])):
        if clause.lower() == english.lower():
            return chinese

    match = re.fullmatch(r"when (?:triggering|activating) (.+)", clause, re.I)
    if match:
        return f"触发 {match.group(1)} 时"
    match = re.fullmatch(r"when comboing from (.+)", clause, re.I)
    if match:
        return f"由 {match.group(1)} 连段而来时"
    match = re.fullmatch(r"when cancelled from (.+)", clause, re.I)
    if match:
        return f"由 {match.group(1)} 招式取消后"
    match = re.fullmatch(r"when using (.+)", clause, re.I)
    if match:
        return f"使用 {match.group(1)} 时"
    match = re.fullmatch(r"during (.+)", clause, re.I)
    if match:
        return f"{match.group(1)} 期间"
    return clause


def _literal_translate(text: str) -> str:
    """Translate remaining short descriptions while preserving move identifiers."""
    replacements = [
        (r"\bCounter Hit state\b", "被康状态"),
        (r"\bcounterhit state\b", "被康状态"),
        (r"\bCounter Hit\b", "康特命中"),
        (r"\bon Counter Hit\b", "康特命中时"),
        (r"\bCounterhit\b", "康特"),
        (r"\bwhiffs?\b", "挥空"),
        (r"\bWhiffs?\b", "挥空"),
        (r"\bcancelled?\b", "取消"),
        (r"\bCancellable\b", "可取消"),
        (r"\bCancelable\b", "可取消"),
        (r"\bcancellable\b", "可取消"),
        (r"\bcancelable\b", "可取消"),
        (r"\bprojectiles?\b", "飞行道具"),
        (r"\bProjectile\b", "飞行道具"),
        (r"\bairborne\b", "空中判定"),
        (r"\bAirborne\b", "空中判定"),
        (r"\bactive frames?\b", "持续帧"),
        (r"\bActive\b", "持续"),
        (r"\brecovery\b", "恢复"),
        (r"\bRecovery\b", "恢复"),
        (r"\bon block\b", "被防御时"),
        (r"\bon hit\b", "命中时"),
        (r"\bon contact\b", "接触到对手时"),
        (r"\bGuard Crush\b", "崩防"),
        (r"\bfrontstep\b", "前垫步"),
        (r"\bbackstep\b", "后垫步"),
        (r"\bdodge\b", "闪避"),
        (r"\bDodge\b", "闪避"),
        (r"\bcombo limit\b", "连击限制"),
        (r"\bdamage scaling\b", "伤害修正"),
        (r"\bDamage Scaling\b", "伤害修正"),
        (r"\bframe advantage\b", "帧数优势"),
        (r"\badvantage\b", "优势帧"),
        (r"\bAdvantage\b", "优势帧"),
        (r"\bstartup\b", "启动"),
        (r"\bStartup\b", "启动"),
        (r"\bcharge\b", "蓄力"),
        (r"\bCharge\b", "蓄力"),
        (r"\barmor\b", "霸体"),
        (r"\bArmored\b", "带霸体"),
        (r"\binvul\b", "无敌"),
        (r"\bInvul\b", "无敌"),
        (r"\bentire move\b", "全招式期间"),
        (r"\bentire duration\b", "全程"),
        (r"\bframe(s)?\b", "帧"),
        (r"\bFrame(s)?\b", "帧"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    return text


def translate(text: str) -> str | None:
    text = _clean(text)
    if not text:
        return None
    if text in NON_NOTES:
        return None
    if text in MANUAL_TRANSLATIONS:
        return MANUAL_TRANSLATIONS[text]
    if "IAS Instant Air Special" in text:
        return "以上数据为最速出招数据，角色一离地就出招（低空必杀技）。"
    match = re.fullmatch(r"Consumes (\d+) Dragon Gauge on frame (\d+)\.?", text)
    if match:
        return f"第{match.group(2)}帧消耗{match.group(1)}点龙之能量。"
    match = re.fullmatch(r"On hit, charge Dragon Gauge for (\d+) \((\d+) if Lyrn is active\)\.?", text)
    if match:
        return f"命中时回复{match.group(1)}点龙之能量（若Lyrn处于激活状态，则回复{match.group(2)}点）。"

    match = re.fullmatch(r"Clash [Ll]evel(?: \(first hit\))?\s*:?\s*(.+?)\.?", text)
    if match:
        prefix = "首段相杀等级为" if "first hit" in text.lower() else "相杀等级为"
        return prefix + match.group(1)

    match = re.fullmatch(r"Slowdown(?: frames)?\s*(?:for)?\s*([\d-]+F?)(?: \(([^)]+)\))?\.?", text, re.I)
    if match:
        frames = match.group(2) or match.group(1)
        return f"慢动作帧：{frames}帧"

    match = re.fullmatch(r"Data in (\[\]|\(\)|\{\}|\[\(\)\]|brackets) (?:denotes|refers? to|is) (.+?)\.?", text, re.I)
    if match:
        marker = "[]" if match.group(1).lower() == "brackets" else match.group(1)
        return f"{marker}括号内为{_translate_data_clause(match.group(2))}的数据"

    match = re.fullmatch(r"Data (?:written|is listed) (?:by version )?as (.+?)\.?", text, re.I)
    if match:
        return f"数据按 {match.group(1)} 版本依次列出"

    match = re.fullmatch(r"(.+?) is in (?:a )?Counter Hit state(?: (.+?))?\.?", text, re.I)
    if match:
        suffix = _clean(match.group(2) or "")
        if not suffix or "entire" in suffix.lower() or "remainder" in suffix.lower():
            return "本招全程处于被康状态"
        if "until frame" in suffix.lower():
            return "本招在" + re.sub(r"until frame\s*", "", suffix, flags=re.I) + "帧前处于被康状态"
        if "frames" in suffix.lower():
            frames = re.sub(r"frames?", "帧", suffix, flags=re.I)
            return f"本招在{frames}处于被康状态"
        if "after invul ends" in suffix.lower():
            return "无敌结束后，本招剩余期间处于被康状态"
        return "本招处于被康状态"

    match = re.fullmatch(r"Counter ?[Hh]it state (?:for )?(?:the )?entire move\.?", text)
    if match:
        return "本招全程处于被康状态"
    match = re.fullmatch(r"(\d+(?:-\d+)?F?) counterhit state\.?", text, re.I)
    if match:
        return f"本招{match.group(1)}处于被康状态"

    match = re.fullmatch(r"(.+?) cannot (dash|block) for (\d+) frames? after recovery ends\.?", text, re.I)
    if match:
        action = "冲刺" if match.group(2).lower() == "dash" else "防御"
        return f"恢复结束后{match.group(3)}帧内不能{action}"

    match = re.fullmatch(r"(.+?) is airborne (?:frames? )?([\d-]+F?)\.?", text, re.I)
    if match:
        return f"本招{match.group(2)}为空中判定"
    match = re.fullmatch(r"Airborne (during active|frames? [\d-]+F?)\.?", text, re.I)
    if match:
        detail = match.group(1)
        return "本招持续帧期间为空中判定" if detail.lower() == "during active" else f"本招{detail[7:]}为空中判定"

    match = re.fullmatch(r"Cancell?able into (.+?) (?:frames? )?(\d+(?:[-~]\d+)?)\.?", text, re.I)
    if match:
        return f"{match.group(2)}帧可取消为 {match.group(1)}"
    match = re.fullmatch(r"Cancell?able into (.+?) on contact only\.?", text, re.I)
    if match:
        return f"仅接触到对手时可取消为 {match.group(1)}"
    match = re.fullmatch(r"Can be cancell?ed into (.+?)\.?", text, re.I)
    if match:
        return f"可取消为 {match.group(1)}"
    match = re.fullmatch(r"Can cancell? into (.+?)\.?", text, re.I)
    if match:
        return f"可取消为 {match.group(1)}"

    match = re.fullmatch(r"Data in (\[\]|\(\)) when (.+?)\.?", text, re.I)
    if match:
        return f"{match.group(1)}括号内为{_translate_data_clause(match.group(2))}的数据"
    match = re.fullmatch(r"(?:True )?[Dd]amage:\s*(.+?)\.?", text)
    if match:
        return f"实际伤害：{match.group(1)}"
    match = re.fullmatch(r"True active:\s*(.+?)\.?", text, re.I)
    if match:
        return f"实际持续帧：{match.group(1)}"
    match = re.fullmatch(r"Total(?: base| minimum)? damage:\s*(.+?)\.?", text, re.I)
    if match:
        return f"总伤害：{match.group(1)}"
    match = re.fullmatch(r"Total\s*:\s*(.+?)\.?", text, re.I)
    if match:
        return f"总伤害：{match.group(1)}"
    match = re.fullmatch(r"Entire maneuver is (.+?) total\.?", text, re.I)
    if match:
        return f"本招总时长为 {match.group(1)}"
    match = re.fullmatch(r"(?:Color|colour):\s*(.+?)\.?", text, re.I)
    if match:
        color = {"N/A": "无", "Green": "绿色", "Blue": "蓝色", "Purple": "紫色"}.get(match.group(1), match.group(1))
        return f"颜色：{color}"
    match = re.fullmatch(r"(-?\d+) on crouch block\.?", text, re.I)
    if match:
        return f"蹲姿防御时为 {match.group(1)}"
    match = re.fullmatch(r"(-?\d+) when jumped post-superflash\.?", text, re.I)
    if match:
        return f"暗转后被跳过时为 {match.group(1)}"
    match = re.fullmatch(r"(?:minimum|Minimum) (\d+F)\.?", text)
    if match:
        return f"最低为 {match.group(1)}"
    match = re.fullmatch(r"(?:Combo launch )?minimum (\d+F)(?: on (Normal Hit|Counter Hit))?\.?", text, re.I)
    if match:
        suffix = {"Normal Hit": "（普通命中时）", "Counter Hit": "（康特命中时）"}.get(match.group(2), "")
        return f"连段最低浮空时间为 {match.group(1)}{suffix}"
    match = re.fullmatch(r"Wallbounces?(?: (.*))?\.?", text, re.I)
    if match:
        suffix = _clean(match.group(1) or "").rstrip(".")
        return "发生墙弹" + (f"（{suffix}）" if suffix else "")
    match = re.fullmatch(r"Data (?:is )?(?:written|formatted|notated) as (.+?)\.?", text, re.I)
    if match:
        return f"数据写法为 {match.group(1)}"

    exact = {
        "Can also be input with j.2U .": "也可输入 j.2U。",
        "Cancellable into c.M on frame 20 on contact only.": "仅接触到对手时可在第20帧取消为 c.M。",
        "Whiffs on crouching opponents.": "对蹲姿对手会挥空。",
        "Beelzebub is in a crouching state during recovery.": "恢复期间贝利尔布处于蹲姿。",
        "Meteorite counts as a separate hit for damage scaling purposes.": "陨石会作为独立攻击计入伤害修正。",
        "Ground bounces on anti-air": "对空命中时会地面弹地。",
        "Not special cancellable.": "不可取消为必杀技。",
        "Combo Launch on Counter Hit": "康特命中时可连段浮空。",
        "Follow-ups will not trigger combo limit": "派生招式不会增加连击限制。",
        "It can only armor one hit.": "本招仅能用霸体承受一次攻击。",
        "The attack itself is not armored.": "攻击部分本身不带霸体。",
        "Can be charged.": "可蓄力。",
        "Can be avoided post-superflash by jumping.": "暗转后可通过跳跃躲避。",
        "Always sideswitches with the opponent on hit": "命中时必定与对手换边。",
        "Forces crouching state on hit.": "命中时强制对手进入蹲姿。",
        "Forces opponent into crouching state on hit.": "命中时强制对手进入蹲姿。",
        "Move unavailable until Jump+3F.": "跳跃后第3帧前无法使用本招。",
        "On block, enters recovery.": "被防御时进入恢复。",
        "Data assumes only the strike connects.": "数据假定仅打击部分命中。",
        "Rising hitbox cannot hit grounded opponents.": "上升阶段的判定无法命中地面上的对手。",
        "Data in [] denotes during .": "[]括号内为该状态下的数据。",
        "2.": "2。",
    }
    if text in exact:
        return exact[text]

    # Do not manufacture mixed Chinese/English notes.  Unhandled prose is
    # deliberately left for a reviewed translation pass.
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write translations to the glossary")
    parser.add_argument(
        "--clear-low-confidence",
        action="store_true",
        help="clear hybrid translations produced by an older literal replacement pass",
    )
    parser.add_argument(
        "--write-review",
        action="store_true",
        help="write remaining untranslated notes to data/gbvsr/notes/_translation_review.json",
    )
    args = parser.parse_args()

    data = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    entries = data["entries"]
    changed = []
    skipped = []
    cleared = []

    for entry in entries:
        text = str(entry.get("text", ""))
        existing = str(entry.get("zh", ""))
        if text in KNOWN_CORRECTIONS:
            entry["zh"] = KNOWN_CORRECTIONS[text]
            existing = entry["zh"]
        if args.clear_low_confidence and existing == _literal_translate(text) and existing != text:
            entry["zh"] = ""
            existing = ""
            cleared.append(text)
        if existing.strip():
            entry["zh"] = _keep_english_names(existing)
            continue
        chinese = translate(text)
        if not chinese:
            skipped.append(text)
            continue
        entry["zh"] = _keep_english_names(chinese)
        changed.append(entry)

    print(f"would_fill={len(changed)}")
    print(f"cleared_low_confidence={len(cleared)}")
    print(f"needs_manual_translation={len(skipped)}")
    for text in skipped[:50]:
        print("REVIEW", text)

    if args.write_review:
        review_path = GLOSSARY_PATH.with_name("_translation_review.json")
        review = {
            "description": "These entries need a reviewed Chinese translation. Parser remnants may remain intentionally blank.",
            "entries": [
                {
                    "text": entry["text"],
                    "count": entry.get("count", 0),
                    "examples": entry.get("examples", []),
                }
                for entry in entries
                if not str(entry.get("zh", "")).strip()
            ],
        }
        review_path.write_text(
            json.dumps(review, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"review_file={review_path}")

    if args.apply:
        GLOSSARY_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote={GLOSSARY_PATH}")


if __name__ == "__main__":
    main()
