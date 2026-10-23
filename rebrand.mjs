// Troca a marca "Twenty" por "Hub Tech" na imagem oficial do Twenty (roda dentro do docker build).
// Só mexe em textos de tela, e-mails, ícones e metadados; nenhuma lógica é alterada.
import fs from 'node:fs';
import path from 'node:path';

const NAME = 'Hub Tech';
const SITE = 'https://credtechdigital.com.br/';
const SOURCE = 'https://github.com/dallabroda/hub-tech';
const ICON_URL = 'https://hub.credtechdigital.com.br/images/hubtech-logo.png';

const SERVER = '/app/packages/twenty-server';
const FRONT = `${SERVER}/dist/front`;
const BRAND = process.argv[2];

// "Twenty" como palavra inteira; não pega identificadores (allowRequestsToTwentyIcons),
// caminhos (/twenty), nem o marcador "BEGIN/END: Twenty Config" que o servidor usa no index.html.
const WORD = /(?<![A-Za-z0-9_$./-])Twenty(?![A-Za-z0-9_$/]| Config)/g;

const URLS = [
  ['https://twentyhq.github.io/placeholder-images/workspaces/twenty-logo.png', ICON_URL],
  ['https://app.twenty.com/images/icons/windows11/Square150x150Logo.scale-100.png', ICON_URL],
  ['https://raw.githubusercontent.com/twentyhq/twenty/main/docs/static/img/social-card.png', ICON_URL],
  ['https://docs.twenty.com/getting-started/introduction', SITE],
  ['https://docs.twenty.com/', SITE],
  ['https://twenty.com/', SITE],
  ['https://github.com/twentyhq/twenty', SOURCE],
];

const stats = { files: 0, words: 0 };

const rewrite = (file, fn) => {
  const before = fs.readFileSync(file, 'utf8');
  const after = fn(before);
  if (after !== before) {
    fs.writeFileSync(file, after);
    stats.files += 1;
  }
};

const replaceBrand = (text) => {
  let out = text.replace(WORD, () => {
    stats.words += 1;
    return NAME;
  });
  for (const [from, to] of URLS) out = out.split(from).join(to);
  return out;
};

const walk = (dir, exts) =>
  fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return walk(full, exts);
    return exts.some((ext) => entry.name.endsWith(ext)) ? [full] : [];
  });

// 1. Frontend: textos de tela (todas as línguas) e links
for (const file of walk(`${FRONT}/assets`, ['.js'])) rewrite(file, replaceBrand);

// 2. E-mails (templates e traduções) e traduções do servidor (assuntos de e-mail, mensagens)
for (const file of walk('/app/packages/twenty-emails/dist', ['.js', '.mjs'])) rewrite(file, replaceBrand);
for (const file of walk(`${SERVER}/dist/engine/core-modules/i18n/locales/generated`, ['.js'])) {
  rewrite(file, replaceBrand);
}

// 3. index.html e manifest: título, descrição, imagem de compartilhamento
rewrite(`${FRONT}/index.html`, (html) =>
  replaceBrand(html)
    .replaceAll('A modern open-source CRM', 'CRM da CredTech')
    .replace('<html lang="en"', '<html lang="pt-BR"'),
);
rewrite(`${FRONT}/manifest.json`, (json) => {
  const manifest = JSON.parse(json);
  manifest.name = NAME;
  manifest.short_name = NAME;
  return `${JSON.stringify(manifest, null, 2)}\n`;
});

// 4. Ícones (mesmos caminhos e tamanhos), logo padrão e logo SVG usado em telas de integração
fs.cpSync(`${BRAND}/icons`, `${FRONT}/images/icons`, { recursive: true });
fs.copyFileSync(`${BRAND}/icon-source.png`, `${FRONT}/images/hubtech-logo.png`);
const png = fs.readFileSync(`${BRAND}/icon-source.png`).toString('base64');
fs.writeFileSync(
  `${FRONT}/images/integrations/twenty-logo.svg`,
  `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 512 512"><image width="512" height="512" xlink:href="data:image/png;base64,${png}"/></svg>\n`,
);

console.log(`Hub Tech: ${stats.words} ocorrências trocadas em ${stats.files} arquivos`);
if (stats.words < 500) {
  // Proteção contra mudança de estrutura numa versão nova do Twenty
  console.error('Poucas ocorrências trocadas: confira os caminhos para esta versão do Twenty');
  process.exit(1);
}
