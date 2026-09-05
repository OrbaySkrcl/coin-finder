# coin-finder — Smart Money Signal & Strategy Lab

## 0. Görev Anlayışım (Understanding)

Referans alınan sistem (AlphaHedgeBot) üç parçadan oluşuyor:

1. **On-chain sinyal motoru** — Kanıtlanmış kârlı ("smart") cüzdanları izler; N tanesi aynı
   tokeni kısa bir pencerede alınca Telegram'a alert atar.
2. **Strategy Lab** — Geçmiş sinyalleri saklayıp filtre kombinasyonlarıyla backtest ettiren
   web paneli.
3. **Para kazanma katmanı** — Telegram aboneliği + opsiyonel otomatik alım (copy trade).

Biz bunun **gerçekten değer üreten** kısmını alıyoruz, pazarlama kısmını değil.

### Referans sistemin dürüst bir okuması (neyi almıyoruz, neden)

Tweet'teki panelde iki sayı yan yana duruyor ve bu ikisi aslında sistemin kendi itirafı:

| Metrik | Tweet'teki değer | Ne demek |
|---|---|---|
| "%50 peak'te çık" | +$251.8k, ROI +%82.5 | **Geriye dönük bakış (look-ahead).** Peak'i ancak sonradan bilirsin. |
| "Reality check: hiç satmamış" | medyan **0.26×**, net +$1.28M ama %81 token flat/rug | Aynı sinyaller, gerçekçi çıkış = sermayenin çoğu yanıyor |
| "Kazanç oranı" %39.9 | 1216 kazanç / 1835 kayıp | Kayıp sayısı kazançtan fazla; her şey kuyruk dağılımına bağlı |

Ayrıca "medyan çarpan 1.54×" **gaz + slippage + swap ücreti + vergi öncesi**. 40k likiditeli bir
tokende $100'lük giriş-çıkışın round-trip maliyeti gerçekte ~%3-8. Bu 1.54× medyanı pratikte
1.4×'e indirir, ve kaybeden trade'lerde kaybı büyütür.

**Kararımız:** Backtest motoru geriye dönük ("peak'te çık") modu **gösterecek ama varsayılan
yapmayacak**. Varsayılan; maliyetli, look-ahead içermeyen, o an alınabilir kararlarla
kurulmuş çıkış modelleri olacak. Bir ürünün asıl değeri sinyalde değil, **sinyalin gerçekte ne
kazandırdığını dürüstçe ölçmesinde.**

### v1 kapsamı (ne inşa ediyoruz)

- Smart-money cüzdan keşfi + sürekli yeniden skorlama (realized PnL, tutarlılık, sybil temizliği)
- Konfluans sinyal motoru (N cüzdan / W pencere) + güvenlik ön-filtresi (honeypot, tax, LP)
- Telegram bot: kişiselleştirilebilir filtreler, alert, /stats, cooldown & spam kontrolü
- Strategy Lab: point-in-time snapshot'lı, maliyet modelli, dürüst backtest + web panel
- Paper trading (forward test): canlı sinyallerin gerçek zamanlı, hilesiz performans karnesi
- Abonelik: HD-türetilmiş kullanıcıya özel deposit adresi (tweet'teki "tag" hilesinden daha temiz)

---

## 1. Mimari Karar Özeti

| Katman | Seçim | Gerekçe |
|---|---|---|
| Dil | **Python 3.11** (asyncio) | Backtest için Polars/NumPy; aiogram olgun; tek dil = tek deploy |
| API | FastAPI + uvicorn | Railway'de tek servis, static SPA'yı da servis eder |
| DB | **PostgreSQL** (Railway addon) | Zaman serisi + ilişkisel; TimescaleDB gerekmez, partition yeter |
| Cache/Queue | **Redis** (Railway addon) | Rate-limit, dedupe, job kuyruğu, pub/sub |
| Backtest | **Polars** | 90 gün × ~50k sinyal × yüzlerce filtre kombosu — pandas'tan ~5-10× hızlı |
| Bot | aiogram 3 | Async, FSM, webhook modu Railway'e uygun |
| Frontend | Vite + React + Recharts | Strategy Lab paneli; build çıktısı FastAPI'den static servis |
| Deploy | Railway (3 servis: api, worker, bot) + Postgres + Redis | Kullanıcının hesabı mevcut |

### Servis topolojisi

```
                    ┌──────────────┐
   RPC / Helius ───▶│  ingest      │  swap event → raw_swaps
   DexScreener  ───▶│  (worker)    │  token meta → tokens, pools
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  scoring     │  realized PnL → wallet_scores
                    │  (cron)      │  sybil cluster → wallet_clusters
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  signal      │  konfluans + güvenlik → signals
                    │  engine      │  (immutable point-in-time snapshot)
                    └───┬──────┬───┘
                        │      │
              ┌─────────▼─┐  ┌─▼──────────────┐
              │ telegram  │  │ api + Strategy │
              │   bot     │  │ Lab (FastAPI)  │
              └───────────┘  └────────────────┘
```

---

## 2. Yapılacaklar (Checklist)

### Faz 0 — İskelet & altyapı
- [ ] `uv` tabanlı Python paket yapısı (`src/coinfinder/...`), pyproject, ruff + mypy
- [ ] Postgres şeması + Alembic migration'ları
- [ ] Config katmanı (pydantic-settings, tüm sırlar env'den)
- [ ] Docker + `railway.toml` (api / worker / bot servisleri)
- [ ] GitHub Actions: ruff, mypy, pytest
- [ ] `docker-compose.yml` ile yerel Postgres+Redis

### Faz 1 — Veri katmanı
- [ ] DexScreener client (rate-limit'li, retry'lı, circuit breaker)
- [ ] GeckoTerminal client (DexScreener fallback + OHLCV backfill)
- [ ] Token/pool metadata cache (Redis + Postgres)
- [ ] Fiyat snapshot yazıcı (sinyal anındaki durumu dondurmak için — **look-ahead önleme**)

### Faz 2 — On-chain ingestion
- [ ] EVM log indexer: Uniswap V2/V3/V4 + Aerodrome + PancakeSwap `Swap` event'leri
- [ ] Reorg-safe checkpoint (N blok confirmation + rollback)
- [ ] Swap → (wallet, token, yön, miktar, USD) normalizasyonu
- [ ] Router/aggregator/MEV bot adres filtresi (1inch, 0x, Banana, Maestro, jaredfromsubway...)
- [ ] Solana ingestion (Helius webhook/gRPC) — *chain kararına bağlı*

### Faz 3 — Smart wallet keşfi & skorlama
- [ ] FIFO realized-PnL hesaplayıcı (kısmi çıkışlar, çoklu giriş)
- [ ] Cüzdan metrikleri: win-rate, medyan çarpan, PnL, işlem sayısı, aktif gün, avg hold
- [ ] Bot/sniper eleme (aynı blokta giriş, >X tx/gün, aynı-nonce paterni)
- [ ] **Sybil/cluster tespiti**: ortak fonlama kaynağı → tek varlık say (sahte konfluansı öldürür)
- [ ] Zaman ağırlıklı skor (yarı ömür ~30 gün) + minimum örneklem eşiği
- [ ] Gün sonu yeniden skorlama cron'u

### Faz 4 — Sinyal motoru
- [ ] Konfluans dedektörü: ≥K farklı *cluster* W penceresinde alım
- [ ] Güvenlik ön-filtresi: honeypot simülasyonu (`eth_call`), buy/sell tax, LP kilit/burn,
      top-holder yoğunlaşması, deployer geçmişi (daha önce rug çekmiş mi)
- [ ] Kalite skoru: **kalibre edilmiş lojistik model** → P(≥2×) — yıldız değil, olasılık
- [ ] Dedupe + cooldown (aynı token için tekrar alert kuralları)
- [ ] Sinyal snapshot'ı immutable yaz (mcap, likidite, yaş, fiyat, holder, tax — o anki hâli)

### Faz 5 — Telegram bot
- [ ] Webhook modu + aiogram router yapısı
- [ ] Alert şablonu (mcap/liq/yaş/tax/kalite/CA + Trojan/Maestro/GMGN deep-link'leri)
- [ ] Kullanıcı filtre profilleri (chain, min cüzdan, mcap aralığı, min likidite, max yaş)
- [ ] `/stats` — kullanıcının kendi filtresinin son 30 gün gerçek performansı
- [ ] Rate limit + mesaj gruplama (spam koruması)

### Faz 6 — Strategy Lab (asıl fark yaratan kısım)
- [ ] Backtest motoru (Polars): filtre kombosu × çıkış modeli → PnL dağılımı
- [ ] **Maliyet modeli**: gaz + DEX fee + likidite derinliğine göre slippage + buy/sell tax
- [ ] Çıkış modelleri: sabit TP merdiveni, trailing stop, süre bazlı, hold-to-now (reality check),
      ve *ayrı işaretlenmiş* peak-based (look-ahead uyarısıyla)
- [ ] Survivorship raporu: delist/rug olan tokenleri **0 olarak** dahil et
- [ ] Bootstrap güven aralığı (medyan ve win-rate için) — tek sayı yerine aralık
- [ ] Overfit uyarısı: en iyi 10 komboyu out-of-sample dilimde de göster
- [ ] FastAPI endpoint'leri + Vite/React panel (mobil öncelikli, screenshot'lardaki gibi)

### Faz 7 — Paper trading / forward test
- [ ] Her sinyal için sanal pozisyon aç, sabit kurallarla kapat, günlük mark-to-market
- [ ] Herkese açık, değiştirilemez performans sayfası (backtest'e karşı gerçeklik kontrolü)

### Faz 8 — Abonelik & ödeme
- [ ] HD wallet (BIP32) ile kullanıcıya özel deposit adresi türetme
- [ ] Deposit watcher → onay sayısı → abonelik aktivasyonu
- [ ] Plan/limit yönetimi, deneme süresi, süre bitiş uyarısı
- [ ] (Tweet'teki "tutar-tag" yöntemi sadece fallback olarak; adres-başına-kullanıcı daha temiz)

### Faz 9 — Otomatik alım (opsiyonel, karara bağlı)
- [ ] Non-custodial öncelikli tasarım; custodial ise KMS/envelope encryption + withdraw allowlist
- [ ] Pozisyon limiti, günlük kayıp limiti, killswitch
- [ ] Simülasyon-önce-gönder (`eth_call`), slippage koruması, private mempool (MEV koruması)

### Faz 10 — Operasyon
- [ ] Structured logging + Sentry
- [ ] Sağlık metrikleri: ingestion gecikmesi, sinyal→alert latency, RPC hata oranı
- [ ] Alarm: veri akışı durursa Telegram'dan admin'e bildirim

---

## 3. Riskler & Dürüst Uyarılar

| Risk | Etki | Azaltma |
|---|---|---|
| **Look-ahead bias** | Backtest gerçekte olmayan kâr gösterir | Point-in-time snapshot, peak modu varsayılan değil |
| **Survivorship bias** | Rug olan tokenler veriden düşer, sonuç şişer | Delist tokenler PnL'e −%100 olarak girer |
| **Overfitting** | "En iyi 10 strateji" geçmişe uydurulmuş olur | Out-of-sample dilim + bootstrap CI zorunlu |
| **Slippage/gaz ihmali** | Lowcap'te tek başına %5-10 fark | Likidite derinliğinden slippage modeli |
| **Sybil konfluans** | Tek kişinin 5 cüzdanı "5 smart wallet" görünür | Fonlama-kaynağı kümelemesi |
| **RPC maliyeti/limiti** | Ingestion durur, sinyal gecikir | Çoklu sağlayıcı + failover + backpressure |
| **Custody riski** | Kullanıcı fonu çalınırsa geri dönüşü yok | v1'de custody yok; olursa KMS + allowlist + limit |
| **Finansal risk (kullanıcı)** | Lowcap memecoin ticareti çoğunlukla zarardır | Panelde gerçek dağılımı göster, "sinyal ≠ tavsiye" |

> **Not:** Bu bir ticaret tavsiyesi sistemi değil, bir veri/analiz aracı. Kullanıcı arayüzünün
> her yerinde gerçek kayıp oranı (referans sistemde bile %60 kayıp) açıkça görünmeli.

---

## 4. Review / Sonuçlar

*(Uygulama ilerledikçe doldurulacak.)*
