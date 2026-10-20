// Pega aquí tu configuración: Firebase Console > Configuración del proyecto > Tus apps > Web
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js";
import {
  getAuth,
  GoogleAuthProvider,
  FacebookAuthProvider,
  signOut,
} from "https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js";

const firebaseConfig = {
  apiKey: "AIzaSyBiPTyMR5zG-eINeXcFaj754LNvCADjk5g",
  authDomain: "bot-telegram-dbe8f.firebaseapp.com",
  projectId: "bot-telegram-dbe8f",
  storageBucket: "bot-telegram-dbe8f.firebasestorage.app",
  messagingSenderId: "774155994110",
  appId: "1:774155994110:web:1ed01bbc2da8b85e6f76af",
};

export const auth = getAuth(initializeApp(firebaseConfig));
export const proveedores = {
  google: new GoogleAuthProvider(),
  facebook: new FacebookAuthProvider(),
};
export const cerrarSesion = () => signOut(auth);
