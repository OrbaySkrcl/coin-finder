# Lessons / Kısıtlar

## 2026-09-05 — Geliştirme sandbox'ında dış ağ kapalı
Bu Claude Code oturumunun egress proxy'si şu hostları **403 policy denial** ile reddediyor:
`api.dexscreener.com`, `api.geckoterminal.com`, `mainnet.base.org`, `*.llamarpc.com`,
`*.publicnode.com`, `*.drpc.org`, `rpc.mainnet.chain.robinhood.com`.

**Sonuç:** Canlı entegrasyon testi burada yapılamaz. Bu yüzden:
- Tüm dış servisler `Protocol` arayüzü arkasına alındı (`sources/`, `rpc/`).
- Testler kayıtlı JSON fixture'larla çalışır (`tests/fixtures/`), ağ gerektirmez.
- Gerçek doğrulama Railway'de (egress açık) `scripts/smoke_check.py` ile yapılır.

**Kural:** Ağ erişimi olmadığı için "çalışıyor" deme. Fixture testi geçti ≠ canlı API uyumlu.
Railway'de ilk deploy sonrası smoke check zorunlu.

## 2026-09-05 — Ücretsiz RPC'de "tüm swapleri indexle" yaklaşımı imkânsız
Base'de ~2s blok süresi × tüm DEX pool'ları = ücretsiz rate limit'i saniyeler içinde yakar.
**Çözüm:** cüzdan-merkezli indeksleme. `eth_getLogs` topic filtresi ERC20 `Transfer`'da
`to` (topic2) ve `from` (topic1) alanlarını indexli tutar, dolayısıyla:

    topics: [Transfer, null, [w1..wN]]   -> izlenen cüzdanlara gelen TÜM token alımları
    topics: [Transfer, [w1..wN], null]   -> izlenen cüzdanlardan çıkan TÜM satışlar

Tek çağrıyla N cüzdanın tüm token hareketi gelir; pool başına sorgu gerekmez.
Bu, ücretsiz katmanda sistemi mümkün kılan tek asıl mimari karar.
