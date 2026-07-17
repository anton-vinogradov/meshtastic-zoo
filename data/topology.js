// meshtastic-zoo — описание топологии.
// Это единственный файл, который нужно править при изменении зоопарка.
// SNR в dB. snr: null + note — линк есть, но не измерен.
window.MESHZOO_DATA = {
  meta: {
    title: "meshtastic-zoo",
    updated: "2026-07-18",
    // Шкала «% от идеала» для раскраски RF-линков: floor → 0%, ideal → 100%
    snrScale: { floor: -20, ideal: 10 }, // dB
  },

  // Зон может быть сколько угодно, порядок в списке = порядок сверху вниз.
  // kind: "subnet" (полоса площадки) | "world" (эфир между ними); h — высота в px (опц.).
  zones: [
    { id: "net77", kind: "subnet", label: "10.77.77.0/24" },
    { id: "world", kind: "world", label: "Внешний мир", h: 900 },
    { id: "net88", kind: "subnet", label: "10.88.88.0/24" },
  ],

  nodes: [
    // Подсеть 77 (верх)
    { id: "FCB",  label: "FCB",        sub: "10.77.77.40", zone: "net77", x: 0.16 },
    { id: "FADV", label: "FADV",       sub: "10.77.77.42", zone: "net77", x: 0.50,
      mobile: true, hint: "перемещается, IP меняется" },
    { id: "FC2",  label: "FC2 · P26",  sub: "10.77.77.41", zone: "net77", x: 0.84 },

    // Внешний мир (середина); x/y — доли от размеров зоны
    { id: "ZHTB", label: "ZHTB ⚓",       sub: "резерв внутри 77", zone: "world", x: 0.48, y: 0.10 },
    { id: "BS",   label: "Black_Salm0n", sub: "резерв МОСТА",     zone: "world", x: 0.60, y: 0.38 },
    { id: "bee0", label: "bee0",         sub: "резерв внутри 88", zone: "world", x: 0.24, y: 0.60 },
    { id: "ptgi", label: "ptgi",         sub: "резерв внутри 88", zone: "world", x: 0.24, y: 0.75 },

    // Подсеть 88 (низ)
    { id: "FCA", label: "FCA",       sub: "10.88.88.40", zone: "net88", x: 0.26 },
    { id: "FC1", label: "FC1 · P23", sub: "10.88.88.41", zone: "net88", x: 0.80 },
  ],

  links: [
    // LAN внутри площадок
    { from: "FCB",  to: "FADV", type: "lan" },
    { from: "FADV", to: "FC2",  type: "lan" },
    { from: "FCA",  to: "FC1",  type: "lan" },

    // Межплощадочный радиомост
    { from: "FC2", to: "FC1", type: "bridge", label: "мост (прямой)" },

    // RF-плечи через внешний мир
    { from: "ZHTB", to: "FCB", type: "rf", snr: 2.5,  labelT: 0.25 },
    { from: "ZHTB", to: "FC2", type: "rf", snr: -2.5, labelT: 0.3 },
    { from: "BS",   to: "FCB", type: "rf", snr: null, note: "плечо не изм.", labelT: 0.35 },
    { from: "BS",   to: "FC2", type: "rf", snr: -8.5,  labelT: 0.35 },
    { from: "BS",   to: "FC1", type: "rf", snr: 5.75,  labelT: 0.55 },
    { from: "BS",   to: "FCA", type: "rf", snr: -14,   labelT: 0.6 },
    { from: "bee0", to: "FCA", type: "rf", snr: 2.75,  labelT: 0.55 },
    { from: "bee0", to: "FC1", type: "rf", snr: -11.75, labelT: 0.25 },
    { from: "ptgi", to: "FCA", type: "rf", snr: -0.5,  labelT: 0.45 },
    { from: "ptgi", to: "FC1", type: "rf", snr: 1.25,  labelT: 0.75 },
  ],
};
