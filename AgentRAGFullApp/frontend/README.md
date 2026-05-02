# NexusAI - Modern AI Chat Interface

Una interfaz de chat moderna y minimalista diseñada para aplicaciones SaaS con IA, inspirada en las mejores prácticas de diseño de OpenAI, Perplexity y otras plataformas líderes.

## ✨ Características

- **Diseño Moderno y Minimalista**: Interfaz limpia con colores vibrantes y gradientes sutiles
- **Modo Oscuro**: Tema oscuro por defecto con transiciones suaves
- **Animaciones Fluidas**: Experiencia de usuario mejorada con Motion (Framer Motion)
- **Sistema de Diseño Cohesivo**: Paleta de colores basada en OKLCH para transiciones suaves
- **Componentes Modulares**: Arquitectura escalable y mantenible
- **Responsive**: Adaptable a diferentes tamaños de pantalla

## 🎨 Paleta de Colores

### Modo Claro
- **Primary**: Púrpura vibrante (`oklch(0.55 0.22 274)`)
- **Background**: Blanco puro con tintes sutiles
- **Accent**: Tonos púrpura claros para elementos secundarios

### Modo Oscuro
- **Primary**: Púrpura brillante (`oklch(0.65 0.24 274)`)
- **Background**: Negro profundo con matices púrpura
- **Accent**: Tonos oscuros con saturación controlada

## 🏗️ Estructura de Componentes

```
src/app/components/
├── Sidebar.tsx          # Navegación lateral con historial de chats
├── Navbar.tsx           # Barra superior con selector de modelos
├── ChatArea.tsx         # Área principal de conversación
├── ChatMessage.tsx      # Componente individual de mensaje
├── MessageInput.tsx     # Input de texto con attachments
└── WelcomeScreen.tsx    # Pantalla de bienvenida animada
```

## 🚀 Tecnologías

- **React 18** - Framework de UI
- **TypeScript** - Tipado estático
- **Tailwind CSS v4** - Framework de estilos
- **Motion (Framer Motion)** - Animaciones
- **Lucide React** - Iconos
- **Radix UI** - Componentes primitivos
- **Sonner** - Notificaciones toast

## 🎯 Casos de Uso

Esta interfaz es perfecta para:

- ✅ Plataformas SaaS con IA
- ✅ Chatbots empresariales
- ✅ Asistentes virtuales
- ✅ Herramientas de productividad con IA
- ✅ Plataformas educativas con IA

## 💡 Características Destacadas

### Sidebar Inteligente
- Historial de conversaciones
- Búsqueda rápida
- Chats fijados
- Tema claro/oscuro

### Selector de Modelos
- Cambio rápido entre modelos de IA
- Indicador visual del modelo activo
- Descripciones informativas

### Área de Chat
- Sugerencias contextuales
- Mensajes con timestamps
- Acciones rápidas (copiar, regenerar, votar)
- Indicador de escritura animado

### Input Avanzado
- Adjuntar archivos
- Grabación de audio
- Auto-resize del textarea
- Sugerencias rápidas
- Atajos de teclado (Enter para enviar)

## 🎨 Personalización

Los colores y estilos se pueden personalizar fácilmente modificando las variables CSS en `/src/styles/theme.css`:

```css
:root {
  --primary: oklch(0.55 0.22 274);
  --gradient-primary: linear-gradient(135deg, ...);
  /* ... más variables */
}
```

## 📝 Notas de Diseño

- **Espaciado consistente**: 4px base unit
- **Bordes redondeados**: 12px (0.75rem) por defecto
- **Sombras sutiles**: Usadas solo para elementos elevados
- **Transiciones**: 200-300ms para hover states
- **Gradientes**: Usados estratégicamente para CTAs y elementos destacados

## 🔮 Próximas Mejoras Sugeridas

- [ ] Sistema de temas personalizables
- [ ] Soporte multi-idioma
- [ ] Exportación de conversaciones
- [ ] Búsqueda avanzada en historial
- [ ] Integración con APIs reales
- [ ] Sistema de plugins/extensiones
- [ ] Modo de colaboración en tiempo real
- [ ] Analytics y métricas

---

Desarrollado con ❤️ para crear la mejor experiencia de IA
