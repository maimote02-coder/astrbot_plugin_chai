"""无课教室图片渲染模板。

把 HTML/CSS 单独拆出来，main.py 只负责查询、整理数据和调用 html_render。
AstrBot 的 html_render 支持 HTML + Jinja2，因此这里可以直接做成类似
Excel 的「楼栋 / 楼层 / 六个大节」矩阵，并使用玻璃拟态视觉。
"""

T2I_TMPL = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <style>
    {{ style | safe }}
  </style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <div class="hero-copy">
        <div class="eyebrow">IBEIKE · CLASSROOM STATUS</div>
        <h1>{{ title }}</h1>
        <p>{{ buildings|length }} 栋楼 · {{ slot_count }} 个大节 · 数据更新于 {{ fetched_at }}</p>
      </div>
      <div class="hero-mark">
        <span>无课</span>
        <strong>ROOMS</strong>
      </div>
    </section>

    {% for b in buildings %}
    <section class="building-card">
      <table class="room-grid">
        <colgroup>
          <col class="col-building">
          <col class="col-floor">
          {% for h in headers %}<col class="col-slot">{% endfor %}
        </colgroup>

        <thead>
          <tr>
            <th class="building-head">楼栋名称</th>
            <th class="floor-head">楼层数</th>
            {% for h in headers %}
            <th class="slot-head">{{ h }}</th>
            {% endfor %}
          </tr>
        </thead>

        <tbody>
          {% for row in b.rows %}
          <tr>
            {% if loop.first %}
            <td class="building-name" rowspan="{{ b.rows|length }}">
              <div class="building-name-inner">
                <span class="building-icon">⌂</span>
                <span>{{ b.name }}</span>
              </div>
            </td>
            {% endif %}

            <td class="floor">{{ row.floor }}层</td>

            {% for c in row.cells %}
            <td class="room-cell{% if not c %} empty{% endif %}">
              {% if c %}
                <div class="rooms">{{ c }}</div>
              {% else %}
                <span class="dash">—</span>
              {% endif %}
            </td>
            {% endfor %}
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </section>
    {% endfor %}

    <footer>
      <span>数据来源：iBeiKe 教务公开接口</span>
      <span>astrbot_plugin_chai</span>
    </footer>
  </main>
</body>
</html>
"""

T2I_STYLE = """
* {
  box-sizing: border-box;
}

html, body {
  margin: 0;
  padding: 0;
}

body {
  width: 1500px;
  padding: 34px 42px 38px;
  color: #25324a;
  font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
  background:
    radial-gradient(circle at 8% 5%, rgba(255,255,255,.95) 0 7%, transparent 22%),
    radial-gradient(circle at 95% 10%, rgba(178,224,255,.62), transparent 25%),
    linear-gradient(135deg, #dcecff 0%, #edf6ff 46%, #e9e1ff 100%);
}

.page {
  width: 100%;
}

.hero {
  min-height: 154px;
  margin-bottom: 22px;
  padding: 28px 32px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border: 1px solid rgba(255,255,255,.78);
  border-radius: 28px;
  background: rgba(255,255,255,.60);
  box-shadow:
    0 18px 48px rgba(91, 112, 155, .16),
    inset 0 1px 0 rgba(255,255,255,.85);
  backdrop-filter: blur(18px);
}

.eyebrow {
  display: inline-block;
  margin-bottom: 9px;
  color: #7085aa;
  font-size: 14px;
  font-weight: 800;
  letter-spacing: 2.4px;
}

h1 {
  margin: 0;
  color: #263a60;
  font-size: 36px;
  line-height: 1.18;
  letter-spacing: .5px;
}

.hero p {
  margin: 11px 0 0;
  color: #73819b;
  font-size: 15px;
}

.hero-mark {
  width: 130px;
  height: 96px;
  padding: 13px 14px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  border-radius: 22px;
  background: linear-gradient(145deg, rgba(118, 183, 255, .82), rgba(161, 137, 246, .72));
  color: white;
  box-shadow: 0 12px 28px rgba(101, 125, 190, .22);
  transform: rotate(2deg);
}

.hero-mark span {
  font-size: 25px;
  font-weight: 900;
  line-height: 1;
}

.hero-mark strong {
  margin-top: 7px;
  font-size: 11px;
  letter-spacing: 2px;
  opacity: .82;
}

.building-card {
  margin: 0 0 20px;
  padding: 12px;
  border: 1px solid rgba(255,255,255,.74);
  border-radius: 23px;
  background: rgba(255,255,255,.48);
  box-shadow:
    0 12px 34px rgba(76, 101, 146, .13),
    inset 0 1px 0 rgba(255,255,255,.85);
  backdrop-filter: blur(16px);
}

.room-grid {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  table-layout: fixed;
  overflow: hidden;
  border: 1px solid rgba(157, 177, 207, .42);
  border-radius: 16px;
  background: rgba(255,255,255,.54);
}

.col-building { width: 132px; }
.col-floor { width: 82px; }
.col-slot { width: auto; }

th, td {
  border-right: 1px solid rgba(164, 181, 207, .38);
  border-bottom: 1px solid rgba(164, 181, 207, .32);
}

tr > *:last-child {
  border-right: 0;
}

tbody tr:last-child > * {
  border-bottom: 0;
}

thead th {
  height: 66px;
  padding: 10px 8px;
  color: #4d5f7e;
  font-size: 18px;
  font-weight: 850;
  text-align: center;
  vertical-align: middle;
  background: rgba(255,255,255,.72);
}

.building-head {
  color: #fff;
  background: linear-gradient(145deg, rgba(116, 179, 241, .96), rgba(117, 139, 224, .90));
}

.floor-head {
  color: #9a6c24;
  background: rgba(255, 231, 164, .72);
}

.slot-head {
  color: #a94f62;
  background: rgba(255, 210, 219, .62);
}

.building-name {
  min-height: 92px;
  padding: 16px 10px;
  color: #243a60;
  background: linear-gradient(180deg, rgba(167, 224, 250, .76), rgba(180, 210, 247, .55));
  vertical-align: middle;
}

.building-name-inner {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 9px;
  font-size: 21px;
  font-weight: 900;
  line-height: 1.25;
  word-break: break-all;
}

.building-icon {
  width: 38px;
  height: 38px;
  display: grid;
  place-items: center;
  border-radius: 12px;
  color: #fff;
  font-size: 21px;
  background: rgba(93, 129, 192, .55);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.4);
}

.floor {
  padding: 14px 7px;
  color: #816a43;
  font-size: 17px;
  font-weight: 800;
  text-align: center;
  vertical-align: middle;
  background: rgba(255, 247, 214, .68);
  white-space: nowrap;
}

.room-cell {
  min-height: 60px;
  padding: 12px 10px;
  color: #314665;
  font-size: 17px;
  font-weight: 650;
  line-height: 1.45;
  text-align: center;
  vertical-align: middle;
  background: rgba(255,255,255,.38);
  overflow-wrap: anywhere;
}

.room-cell:nth-child(4n) {
  background: rgba(246, 250, 255, .42);
}

.room-cell.empty {
  color: #aeb8c8;
  background: rgba(238, 243, 249, .34);
}

.rooms {
  white-space: normal;
  word-break: break-word;
}

.dash {
  display: inline-block;
  font-size: 18px;
  font-weight: 400;
  opacity: .65;
}

footer {
  padding: 4px 10px 0;
  display: flex;
  justify-content: space-between;
  color: #77849a;
  font-size: 12px;
}

footer span:last-child {
  opacity: .65;
}
"""

RENDER_OPTIONS = {
    "type": "jpeg",
    "quality": 92,
    "full_page": True,
    "animations": "disabled",
    "caret": "hide",
    "scale": "css",
}
