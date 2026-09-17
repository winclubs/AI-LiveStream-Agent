const fs = require('fs');
const path = require('path');

const files = [
  'server/static/svg/tts_cosyvoice.svg',
  'server/static/svg/tts_edgetts.svg',
  'server/static/svg/tts_chattts.svg',
  'server/static/svg/tts_gptsovits.svg',
  'server/static/svg/tts_elevenlabs.svg',
  'server/static/svg/tts_custom.svg'
];

console.log("=== TTS 引擎 SVG 尺寸与盛满一致性校验 ===");
let allPass = true;

files.forEach(fileRel => {
  const fullPath = path.join(__dirname, '..', fileRel);
  if (!fs.existsSync(fullPath)) {
    console.error(`[FAIL] 文件不存在: ${fileRel}`);
    allPass = false;
    return;
  }
  const content = fs.readFileSync(fullPath, 'utf8');
  const hasViewBox48 = content.includes('viewBox="0 0 48 48"');
  const hasWidth48 = content.includes('width="48"');
  const hasHeight48 = content.includes('height="48"');
  const hasRx12 = content.includes('rx="12"');
  
  const ok = hasViewBox48 && hasWidth48 && hasHeight48 && hasRx12;
  if (!ok) allPass = false;
  console.log(`${ok ? '✅ [PASS]' : '❌ [FAIL]'} ${fileRel}: viewBox48=${hasViewBox48}, w48=${hasWidth48}, h48=${hasHeight48}, rx12=${hasRx12}`);
});

if (allPass) {
  console.log("\n🎉 全部 6 个 TTS 引擎 Logo 图标规格 100% 统一（48x48，rx=12 盛满无缩边）！");
} else {
  console.error("\n存在未统一的图标规格！");
  process.exit(1);
}
