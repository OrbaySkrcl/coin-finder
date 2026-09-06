# Kullanım Rehberi

Kurulum bittiyse ([Kurulum Rehberi](BASLANGIC.md)) burada sistemi günlük olarak
nasıl kullanacağınız var.

---

## 1. Telegram komutları

Botunuza yazacağınız komutların tamamı bu kadar:

| Komut | Ne yapar |
|---|---|
| `/start` | Botu açar, deneme sürenizi başlatır |
| `/durum` | **En önemlisi.** Sistem çalışıyor mu, hangi aşamada, sorun var mı |
| `/ayarlar` | İşlem boyutu, zincirler, maliyet tavanı, filtreler |
| `/stats` | **Sizin filtrenizin** geçmişte gerçekte ne kazandırdığı |
| `/top` | En yüksek puanlı cüzdanlar |
| `/pause` | Bildirimleri durdurur |
| `/resume` | Bildirimleri tekrar açar |

Bir sorun olduğunu düşündüğünüzde ilk yazacağınız şey `/durum`.

---

## 2. Bir sinyal nasıl okunur

Gelen bildirim şuna benzer:

```
🟢 BUY SIGNAL · #Base

💎 COLLECT
🧠 Smart wallets: 5 → 3 independent
💰 MCAP: $190,9b
💧 Liquidity: $40,8b
📊 Liq/MC: %21,4
🕐 Age: 1h 50m
💵 Price: $0,000197
🛒 Smart money in: $1,2b

🎯 P(reaches 2x): %34
💸 Round trip at $100: %1,6 → need 1,02x to break even
🛡 🟡 Caution
   • LP not burned
   • owner not renounced

0xa1243aa393fe014b65a0d925a54a0165385ae26d
```

Satır satır:

**`🧠 Smart wallets: 5 → 3 independent`**
Bu satır bu sistemin diğerlerinden ayrıldığı yer. 5 farklı cüzdan adresi bu
tokeni aldı, **ama bunların sadece 3'ü birbirinden bağımsız.** Kalan 2'si
aynı kişinin başka cüzdanı. Sistem bunu, aynı tokenleri saniyeler arayla
tekrar tekrar alan cüzdanları eşleştirerek tespit ediyor.

Neden önemli? Çünkü bir token ekibinin sahte "5 akıllı cüzdan aldı" sinyali
üretmesinin en ucuz yolu, 5 cüzdan açıp aynı anda almaktır. **Siz 3 rakamına
bakın, 5'e değil.**

**`📊 Liq/MC: %21,4`**
Likiditenin market cap'e oranı. Yüksek olması iyidir — çıkmak kolay demektir.
%2'nin altı ciddi bir uyarı işaretidir: fiyat ekranda görünüyordur ama o
fiyattan satamazsınız.

**`🎯 P(reaches 2x): %34`**
Bu tokenin 2 katına çıkma olasılığı. Yıldız değil, **kontrol edilebilir bir
sayı**. %34 dediyse, böyle 100 sinyalden yaklaşık 34'ünün 2x yapması beklenir.
Yapmıyorsa model yanlıştır ve bunu ölçebilirsiniz — yıldızlarla bunu yapamazsınız.

> İlk 400 sinyal sonuçlanana kadar bu sayı bir **tahmin modelinden** gelir,
> ölçümden değil. Veri birikince model gerçek sonuçlara göre yeniden ayarlanır.

**`💸 $10 için gidiş-dönüş: %1,1 → başabaş 1,011x`**
**Alerttteki en değerli satır bu.** `/ayarlar`'da seçtiğiniz işlem boyutuna
göre hesaplanır. $10'luk bir işlemde girip çıkmanın toplam maliyeti %1,1;
zarar etmemek için tokenin 1,011 katına çıkması yeterli.

Aynı tokende, farklı boyutlarda:

| Pozisyon | Maliyet | Başabaş |
|---|---|---|
| $10 | %1,1 | 1,011x |
| $100 | %1,6 | 1,017x |
| $500 | %5,3 | 1,056x |
| $5.000 | %33,4 | **1,562x** |

Havuz sığ olduğu için büyük emir kendi fiyatını yukarı itiyor, çıkarken de
aşağı. **Bu tek tablo, çoğu insanın neden kâr eden sinyallerde bile para
kaybettiğinin cevabıdır.**

Bazen altında şu satır da çıkar:

`⛽ bunun %2,4 puanı gaz — bu boyutta BNB Chain pahalı`

Bu, maliyetin çoğunun gazdan geldiği anlamına gelir. Çözümü filtre değiştirmek
değil, **o zinciri kapatmak veya pozisyonu büyütmek.**

`ℹ️ token vergisi bu hesaba dahil değil (ölçülemiyor)` satırı her alertte var.
Ücretsiz altyapıda vergi simülasyonu yapılamıyor ve küçük pozisyonda en büyük
maliyet kalemi bu — bu yüzden sessiz geçilmiyor. Bölüm 2b'deki 10 saniyelik
kontrolü yapın.

**`🛡 🟡 Caution`**
Güvenlik değerlendirmesi:

| İşaret | Anlamı |
|---|---|
| 🟢 temiz | Kontroller geçti: satışlar görülüyor, LP yakılmış, sahiplik bırakılmış |
| 🟡 dikkat | Bir veya birkaç uyarı var — altındaki maddeleri okuyun |
| 🔴 riskli | Engellendi, size gönderilmez |

> **Robinhood Chain için not:** Bu zincirde honeypot/vergi taraması yapan
> üçüncü parti servis yok. Sistem bu zincirde **asla "temiz" demez**, her
> zaman "dikkat" der ve bunu açıkça yazar. Bilerek böyle: bilmediğimiz bir
> şeye güvenli demek en kötü yalandır.

---

## 2b. Küçük pozisyonla ($5-20) çalışıyorsanız

Bu boyutta maliyet yapısı tamamen değişiyor. Aşağıdakiler sistemin kendi
maliyet modelinden hesaplanmış gerçek sayılar, $40b likiditeli bir havuz için:

| Pozisyon | Base | Robinhood | BNB Chain |
|---|---|---|---|
| $5 | %1,45 | %1,05 | **%5,45** |
| $10 | %1,10 | %0,90 | **%3,10** |
| $20 | %1,00 | %0,90 | **%2,00** |
| $100 | %1,62 | %1,60 | %1,82 |
| $500 | %5,35 | %5,34 | %5,39 |

**Üç sonuç çıkıyor:**

**1. Bu boyutta baskın maliyet kayma değil, gaz.** $10'luk bir işlemde:

| Zincir | Toplam | Gaz | DEX ücreti | Kayma |
|---|---|---|---|---|
| Base | %1,10 | %0,40 | %0,60 | %0,10 |
| BNB Chain | **%3,10** | **%2,40** | %0,60 | %0,10 |

Gaz işlem başına alınır, dolara göre değil. BNB Chain gazı Base'in ~6 katı
olduğu için bu boyutta 3 kat pahalı. **$5-20 çalışıyorsanız BNB Chain'i
kapatın.** Aynı sinyal Base'de gelirse alın, BNB'de gelirse maliyet yer.

**2. $5-20 aslında optimuma yakın.** Maliyet eğrisinin dibi burada:

| Havuz likiditesi | En ucuz pozisyon | O boyutta maliyet |
|---|---|---|
| $3b | $6 | %2,06 |
| $10b | $10 | %1,40 |
| $40b | $20 | %1,00 |
| $150b | $39 | %0,81 |

Bakiyeniz artınca pozisyonu büyütmek *otomatik olarak* iyi değil. $500'e
çıkarsanız maliyet %1'den %5,3'e fırlar — beş kat. Doğru büyüme yolu
**pozisyon başına miktarı değil, pozisyon sayısını artırmak.**

**3. En büyük tehlike vergi.** $10 pozisyonda, $40b likidite:

| Al/sat vergisi | Gidiş-dönüş | Başabaş |
|---|---|---|
| %0 | %1,10 | 1,011x |
| %3 | %6,99 | 1,075x |
| %5 | **%10,81** | **1,121x** |
| %10 | **%20,02** | **1,250x** |

%5/%5 vergili bir token, temiz bir tokenden **10 kat** pahalı. Ve bot bunu
ölçemiyor — ücretsiz RPC'de vergi simülasyonu yapılamıyor, o yüzden her
alertte "token vergisi bu hesaba dahil değil" yazıyor.

> **Alım öncesi 10 saniyelik kontrol:** kontrat adresini kopyalayıp
> [honeypot.is](https://honeypot.is) veya
> [tokensniffer.com](https://tokensniffer.com) sitesine yapıştırın. Al/sat
> vergisini gösterir. %5'in üstündeyse bu boyutta almayın.

**4. Sığ havuzdan kaçının.** $10 pozisyon, Base:

| Likidite | Gidiş-dönüş | Başabaş |
|---|---|---|
| $500 | %8,37 | 1,091x |
| $1b | %4,83 | 1,051x |
| $3b | %2,31 | 1,024x |
| $8b | %1,49 | 1,015x |
| $20b+ | %1,20 | 1,012x |

$5b altındaki havuzlarda maliyet hızla tırmanıyor.

### Önerilen ayarlar ($5-20 için)

`/ayarlar` komutundan:

| Ayar | Değer | Neden |
|---|---|---|
| İşlem başına | **$10** veya **$20** | Maliyet eğrisinin dibi |
| Zincirler | **Base + Robinhood**, BNB kapalı | BNB gazı bu boyutta 3 kat pahalı |
| Min bağımsız cüzdan | **3** | Denge noktası |
| Maliyet tavanı | **%2** | Pahalı sinyalleri hiç göstermez |
| Riskli tokenleri ele | **AÇIK** | $10'da bile rug $10 kaybettirir |
| Market cap | **<500b** | Yükselme alanı bırakır |

**Maliyet tavanı** en işinize yarayacak yeni ayar: seçtiğiniz işlem boyutunda
gidiş-dönüş maliyeti tavanı aşan sinyaller size hiç gönderilmez. $10 ile
çalışıp %2 tavan koyarsanız, sığ havuzlu ve pahalı zincirdeki sinyaller
otomatik olarak elenir.

---

## 3. Filtreleri ayarlama

`/filters` yazın. Karşınıza düğmeler çıkar:

**Zincirler** — Hangi ağlardan sinyal alacağınız. Hepsi açık gelir.

**Minimum cüzdan (2+ / 3+ / 4+ / 5+)** — Kaç *bağımsız* akıllı cüzdanın
alması gerektiği.

| Ayar | Sonuç |
|---|---|
| 2+ | Çok sinyal, çok gürültü |
| **3+** | **Varsayılan. Dengeli.** |
| 4+ | Daha az ama daha güçlü |
| 5+ | Çok az sinyal, en yüksek kanaat |

**Market cap üst sınırı** — `<100b`, `<500b`, `<2M` veya sınırsız.
Düşük market cap = daha çok yükselme alanı ama daha çok risk. Tweet'teki
"20-35b altı lowcap" stratejisi burada `<100b` seçmeye karşılık gelir.

**Riskli tokenleri ele** — Açıkken güvenlik kontrolünden geçemeyen tokenler
size hiç gelmez. **Açık bırakmanızı öneririm.**

---

## 4. `/stats` — kendi filtrenizin karnesi

Bu komut, **sizin ayarladığınız filtrenin** son 30 günde gerçekte ne
kazandırdığını gösterir. Reklam değil, kendi verinizden hesaplanır.

```
Sizin filtreniz, son 30 gün
3+ cüzdan / MC 0-500b
Çıkış kuralı: kademeli sat · ücret, kayma ve gaz düşülmüş

Sinyal: 247
Kazanç oranı: %31,2 (%95 aralık %25,6–%37,2)
Medyan: 0,84x (%95 aralık 0,71–0,98)
Getiri: -%12,4 / $24.700 yatırıma
Sıfırlanan token: %27
Gidiş-dönüş maliyet: %2,1
```

**`%95 aralık` ne demek?** Kazanç oranı %31,2 çıktı ama gerçek değer
%25,6 ile %37,2 arasında bir yerde. Az sinyalle bu aralık çok geniş olur.
**Tek bir sayıya değil, aralığa bakın.** Aralık genişse, o sayı henüz
bir şey ifade etmiyor demektir.

**`Sıfırlanan token: %27`** — Sinyallerin %27'si tamamen değersizleşti.
Bunlar hesaba **−%100 olarak** dahil edildi, listeden silinmedi. Çoğu ürün
bunları sessizce düşürür ve sonuçlar olduğundan iyi görünür.

---

## 5. Strateji Lab (web paneli)

Panel adresini tarayıcıda açın. Beş bölüm var:

### Sistem durumu
En üstte. Her şeyin yolunda olup olmadığı. Günde bir bakmanız yeterli.

### Çıkış kuralı karşılaştırması
**Panelin en öğretici tablosu.** Aynı sinyaller, aynı para, tek fark:
ne zaman sattığınız.

```
Çıkış kuralı        Sinyal   Kazanç    Medyan   Getiri
2x'te sat            1.230    %26,1    0,27x    -%36,9
4 saat sonra sat     1.230    %38,0    0,84x    +%66,0
hiç satma            1.230    %24,4    0,27x    -%15,1
zirvenin yarısında   1.230    %19,0    0,43x    +%31,8   [geriye dönük]
```

Fark %100'den fazla — **sadece çıkış kuralından.** Hangi tokeni aldığınızdan
çok, ne zaman sattığınız belirliyor.

**`geriye dönük` etiketi çok önemli.** "Zirvenin yarısında sat" kulağa iyi
geliyor ama zirveyi ancak *sonradan* bilebilirsiniz. Gerçek zamanda bu kuralı
uygulayamazsınız. Bu yüzden bu satırlar bir **tavan**, ulaşabileceğiniz bir
sonuç değil.

> İncelediğimiz referans ürünün manşetteki "+%82,5 getiri" rakamı tam olarak
> bu tür bir kuraldan geliyor. Aynı sinyallere gerçekçi kurallar uygulandığında
> sonuç −%46 ile +%80 arasında değişiyor.

### Kendi filtreni test et
Filtre ve çıkış kuralı seçip **Testi çalıştır** deyin. Altta çıkan:

- **Sonuç dağılımı** — kaç token zararda kaldı, kaçı 2-5x yaptı. Kırmızı çubuk
  genelde en uzun olanıdır. Bu normaldir ve gerçektir.
- **Test dönemi kontrolü** — dönem ikiye bölünür. İlk yarıda ayarlanan bir
  filtre ikinci yarıda da işe yarıyor mu? **Yaramıyorsa o filtre geçmişe
  uydurulmuş demektir.**

### Strateji sıralaması
576 filtre kombinasyonu otomatik denenir ve sıralanır.

**Sadece "Test dönemi" sütununa bakın.** "Getiri" sütununda birinci olup test
döneminde çöken bir kombinasyon işe yaramaz — geçmişe uydurulmuştur.

Sıralama, az sinyale dayanan sonuçları kasıtlı olarak cezalandırır. 30 sinyalle
%200 getiri, 400 sinyalle %90 getiriden daha az güvenilirdir.

### Son sinyaller
Ham liste. Zirve ve şu anki durum yan yana.

---

## 6. Sık yapılan hatalar

**Manşet rakama bakıp aralığı atlamak.**
"%45 kazanç oranı" 40 sinyale dayanıyorsa gerçek değer %30 ile %60 arasında
olabilir. Aralık her zaman yazıyor.

**Pozisyon boyutunu havuz derinliğine göre ayarlamamak.**
Aynı strateji $100'de kâr, $5.000'de zarar edebilir. Her alertteki
"round trip" satırı bunu size söylüyor.

**"Geriye dönük" etiketli sonuçlara göre karar vermek.**
Bunlar neyin mümkün olduğunun üst sınırı, neyin elde edilebileceği değil.

**Az sinyalli bir filtreyi "en iyi strateji" sanmak.**
Filtreyi daralttıkça geçmiş veride harika görünen kombinasyonlar bulursunuz.
Test dönemi sütunu bunun gerçek olup olmadığını söyler.

**Her sinyalin kazandıracağını sanmak.**
Sinyallerin çoğu zarar eder. Sistem bunu gizlemiyor — dağılım grafiği her
testin altında duruyor. Bu araç size *hangisinin* daha iyi bir bahis olduğunu
söyler, hepsinin kazanacağını değil.

---

## 7. Ayar değiştirme

Railway → servis → **Variables** sekmesinden değiştirilir. Kaydettiğinizde
sistem kendini yeniden başlatır (~2 dakika).

**Sinyal az geliyorsa:**

| Değişken | Varsayılan | Deneyin |
|---|---|---|
| `CONFLUENCE_MIN_CLUSTERS` | `3` | `2` — daha çok sinyal, daha çok gürültü |
| `CONFLUENCE_WINDOW_MINUTES` | `180` | `360` — daha geniş zaman penceresi |
| `MIN_LIQUIDITY_USD` | `5000` | `3000` — daha küçük havuzlara izin ver |

**Sinyal çok geliyorsa:** aynı değerleri ters yönde değiştirin.

**Aynı token için tekrar tekrar bildirim geliyorsa:**
`SIGNAL_COOLDOWN_MINUTES` değerini `360`'tan yükseltin.

---

## 8. Yedekleme

Sinyaller ve sonuçları zamanla birikir ve **geri getirilemez** — o anki
market cap, likidite ve yaş bilgisi sonradan hesaplanamaz.

Railway → **Postgres** kutusu → **Data** sekmesinden düzenli yedek alın.
Ayda bir yeterli.

---

## Sorun mu var?

1. Telegram'da `/durum` yazın
2. Kırmızı satır varsa ne dediğini okuyun — genelde hangi ayarın eksik
   olduğunu doğrudan söyler
3. Çözülmezse `/durum` çıktısının ekran görüntüsünü alıp haber verin
