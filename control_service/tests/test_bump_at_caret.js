// bumpAtCaret 单元测试（node，无框架）
// 运行: node devices/generators/adiclockevals/control_service/tests/test_bump_at_caret.js
"use strict";
const assert = require("assert");
const { bumpAtCaret } = require("../static/app.js");

const cases = [
  // [输入, caret, dir, min, max, 期望]
  ["218.50", 3, 1, null, null, "219.50"],   // 个位 +1
  ["218.50", 3, -1, null, null, "217.50"],  // 个位 -1
  ["218.50", 1, 1, null, null, "318.50"],   // 百位 +1
  ["218.50", 2, 1, null, null, "228.50"],   // 十位 +1
  ["218.50", 5, 1, null, null, "218.60"],   // 十分位 +1
  ["218.50", 6, 1, null, null, "218.51"],   // 百分位 +1
  ["99", 2, 1, null, null, "100"],          // 进位增加一位
  ["100", 3, -1, null, null, "99"],         // 借位减少一位
  ["9.9", 3, 1, null, null, "10.0"],        // 跨小数点进位
  ["1.0", 3, -1, null, null, "0.9"],        // 跨小数点借位
  ["-0.5", 2, -1, null, null, "-1.5"],      // 负数借位（个位）
  ["-0.5", 4, -1, null, null, "-0.6"],      // 负数小数位
  ["199", 3, 1, null, null, "200"],         // 进位连续
  ["200", 3, -1, null, null, "199"],        // 借位连续
  ["218", 3, 1, null, null, "219"],         // 整数个位
  ["0.30", 4, -1, null, null, "0.29"],      // 浮点无噪声（百分位）
  ["0.98", 4, 1, 0, 1, "0.99"],             // clamp 边界内
  ["0", 1, -1, 0, null, null],              // freq 下界：0 不能再减
  ["1.00", 3, 1, 0, 1, null],               // amp 上界：1.00 不能再加
  ["218.50", 4, 1, null, null, null],       // 竖线左侧是小数点 → 忽略
  ["218.50", 0, 1, null, null, null],       // 竖线在最左 → 忽略
  ["abc", 1, 1, null, null, null],          // 左侧非数字 → 忽略
  ["", 1, 1, null, null, null],             // 空串 → null
  ["1e5", 2, 1, null, null, null],          // 科学计数法 → 忽略
  ["2.718", 1, 1, null, null, "3.718"],     // 个位 +1（保留 3 位小数）
  ["2.718", 2, 1, null, null, null],        // 竖线左侧是小数点 → 忽略
];

let failed = 0;
for (const [text, caret, dir, min, max, expected] of cases) {
  const got = bumpAtCaret(text, caret, dir, min, max);
  if (got !== expected) {
    failed++;
    console.error("FAIL: bumpAtCaret(%o, %d, %d, %o, %o) = %o, expected %o",
      text, caret, dir, min, max, got, expected);
  }
}
if (failed) {
  console.error("%d/%d 用例失败", failed, cases.length);
  process.exit(1);
}
console.log("全部 %d 个用例通过", cases.length);
