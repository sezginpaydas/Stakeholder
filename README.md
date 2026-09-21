# Stakeholder
Stakeholder is lightweight Kanban task board for small teams — assignments, notifications, time tracking &amp; encrypted text sharing. / Ekipler için hafif Kanban görev panosu.

## Kurulum / Setup

Gereksinim: Windows ve Python 3.10+ (ek paket gerekmez).

1. `teams.example.json` dosyasını `teams.json` adıyla kopyalayın; ekip adlarını, kullanıcı adlarını, şifreleri ve üyeleri doldurun.
2. `baslat.bat` ile başlatın, `durdur.bat` ile durdurun. Uygulama `http://localhost:8080` adresinde açılır; aynı ağdakiler `http://<bilgisayar-ip>:8080` ile erişebilir.

`teams.json`, veri dosyaları (`data*.json`), `secret.key` ve `backups/` içeriği yerelde kalır, repoya gönderilmez.

---

Requires Windows and Python 3.10+ (no extra packages). Copy `teams.example.json` to `teams.json` and fill in your teams, then run `baslat.bat` (start) / `durdur.bat` (stop). Local data, secrets and backups are git-ignored.
