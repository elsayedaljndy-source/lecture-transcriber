import type { CapacitorConfig } from '@capacitor/cli';

const backendUrl = process.env.BACKEND_URL || 'https://YOUR-RENDER-APP.onrender.com';

const config: CapacitorConfig = {
  appId: 'com.lecture.transcriber',
  appName: 'مُفرّغ المحاضرات',
  webDir: 'static',
  server: {
    url: backendUrl,
    cleartext: false,
    androidScheme: 'https'
  },
  android: {
    allowMixedContent: false,
    backgroundColor: '#0b0f14'
  }
};

export default config;
