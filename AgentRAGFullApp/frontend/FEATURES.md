# NexusAI - Características Detalladas

## 🎨 Sistema de Diseño

### Paleta de Colores AI-First

La aplicación utiliza un esquema de colores moderno basado en **púrpura** (hue 274°) que evoca tecnología, innovación y futuro. Todos los colores están definidos en el espacio de color **OKLCH** para garantizar transiciones suaves y consistencia perceptual.

#### Colores Principales

**Modo Claro:**
- Primary: `oklch(0.55 0.22 274)` - Púrpura vibrante
- Background: `oklch(0.99 0 0)` - Blanco puro
- Muted: `oklch(0.95 0.005 274)` - Gris claro con tinte púrpura

**Modo Oscuro:**
- Primary: `oklch(0.65 0.24 274)` - Púrpura brillante
- Background: `oklch(0.12 0.01 274)` - Negro profundo con tinte púrpura
- Card: `oklch(0.15 0.01 274)` - Superficie elevada

### Gradientes

Se utilizan gradientes sutiles para elementos destacados:

```css
--gradient-primary: linear-gradient(135deg, 
  oklch(0.65 0.24 274) 0%, 
  oklch(0.6 0.2 290) 100%
);
```

### Tipografía

- **Fuente Principal**: Inter (sistema por defecto)
- **Peso Normal**: 400
- **Peso Medium**: 500
- **Escala de tamaños**: Base 16px con escalado fluido

## 🧩 Componentes

### 1. Sidebar (Barra Lateral)

**Ubicación**: `/src/app/components/Sidebar.tsx`

**Características:**
- ✅ Header con logo animado (Sparkles icon)
- ✅ Botón de nueva conversación con gradiente
- ✅ Búsqueda en tiempo real con filtrado
- ✅ Secciones de chats fijados y recientes
- ✅ Toggle de modo claro/oscuro
- ✅ Botones de archivados y configuración

**Animaciones:**
- Entrada lateral con slide-in
- Hover effects en items de chat
- Scale animation al hacer clic

**Datos Mock:**
- 7 conversaciones de ejemplo
- 2 chats fijados
- Timestamps en español

### 2. Navbar (Barra Superior)

**Ubicación**: `/src/app/components/Navbar.tsx`

**Características:**
- ✅ Selector de modelos AI con dropdown
- ✅ Indicador de estado activo (punto pulsante)
- ✅ Título de conversación dinámico
- ✅ Botones de compartir y configuración
- ✅ Backdrop blur effect

**Modelos Disponibles:**
1. GPT-4 Turbo - "Más rápido y preciso"
2. GPT-4 - "Razonamiento avanzado"
3. Claude 3 Opus - "Análisis profundo"
4. Gemini Pro - "Multimodal"

**Animaciones:**
- Slide down al cargar
- Smooth transitions en hover

### 3. ChatArea (Área de Conversación)

**Ubicación**: `/src/app/components/ChatArea.tsx`

**Características:**
- ✅ Pantalla de bienvenida con sugerencias
- ✅ 4 tarjetas de acciones sugeridas
- ✅ Lista de mensajes con scroll
- ✅ Indicador de carga animado

**Estado Vacío:**
- Hero section con icono de Sparkles
- Título con gradiente de texto
- 4 sugerencias en grid 2x2:
  - ¿Qué es la inteligencia artificial?
  - Ayúdame a programar
  - Ideas creativas
  - Análisis de datos

**Indicador de Carga:**
- Avatar del bot con icono rotando
- 3 puntos pulsantes
- Animación secuencial

### 4. ChatMessage (Mensaje Individual)

**Ubicación**: `/src/app/components/ChatMessage.tsx`

**Características:**
- ✅ Avatar diferenciado (Usuario vs AI)
- ✅ Timestamp opcional
- ✅ Acciones contextúales (solo para AI):
  - Copiar mensaje
  - Thumbs up/down
  - Regenerar respuesta

**Estilos:**
- Mensajes del usuario: fondo transparente
- Mensajes de AI: fondo sutil (muted/30)
- Acciones visibles en hover

### 5. MessageInput (Input de Mensajes)

**Ubicación**: `/src/app/components/MessageInput.tsx`

**Características:**
- ✅ Textarea con auto-resize
- ✅ Sugerencias rápidas (cuando está vacío)
- ✅ Botón de adjuntar archivos
- ✅ Grabación de audio con indicador
- ✅ Botón de enviar/detener
- ✅ Soporte para Enter (enviar) y Shift+Enter (nueva línea)

**Sugerencias Rápidas:**
- "Ayúdame con código" (icono Code, azul)
- "Genera una imagen" (icono Image, púrpura)

**Estados:**
- Normal: botón de enviar con gradiente
- Cargando: botón de detener (rojo)
- Grabando: indicador pulsante rojo

**Texto de Ayuda:**
"NexusAI puede cometer errores. Verifica la información importante."

### 6. SettingsPanel (Panel de Configuración)

**Ubicación**: `/src/app/components/SettingsPanel.tsx`

**Características:**
- ✅ Drawer lateral deslizable
- ✅ 4 secciones principales:
  - Cuenta
  - Notificaciones
  - Interfaz
  - Privacidad
- ✅ Switches para cada opción
- ✅ Acciones rápidas adicionales
- ✅ Botones de guardar/cancelar

**Animaciones:**
- Slide-in desde la derecha
- Spring animation suave
- Backdrop blur con fade

## 🎭 Animaciones

### Motion (Framer Motion)

Todas las animaciones utilizan la biblioteca Motion para garantizar fluidez de 60fps.

**Tipos de Animaciones:**

1. **Entrada de Componentes:**
   ```tsx
   initial={{ opacity: 0, y: 20 }}
   animate={{ opacity: 1, y: 0 }}
   transition={{ duration: 0.3 }}
   ```

2. **Hover Effects:**
   ```tsx
   whileHover={{ scale: 1.01 }}
   whileTap={{ scale: 0.99 }}
   ```

3. **Animaciones Infinitas:**
   ```tsx
   animate={{ rotate: 360 }}
   transition={{ repeat: Infinity, duration: 2 }}
   ```

4. **Secuenciales:**
   ```tsx
   transition={{ delay: index * 0.1 }}
   ```

## 🔔 Notificaciones (Toast)

Utiliza **Sonner** para notificaciones elegantes:

- ✅ Success: "Nueva conversación iniciada"
- ✅ Success: "Enlace copiado al portapapeles"
- ✅ Info: "Generación detenida"
- ✅ Info: "Configuración próximamente"

**Posición:** Top-center
**Duración:** Auto (default)

## 🌓 Modo Oscuro

**Implementación:**
- Estado manejado en App.tsx
- Clase `.dark` en `<html>`
- Toggle en Sidebar
- Persiste en localStorage (próximamente)

**Características:**
- Transiciones suaves entre modos
- Colores optimizados para cada modo
- Contraste WCAG AA compliant

## 📱 Responsive Design

**Breakpoints:**
- Mobile: < 768px
- Tablet: 768px - 1024px
- Desktop: > 1024px

**Adaptaciones:**
- Sidebar colapsable en mobile
- Grid de sugerencias: 1 columna en mobile, 2 en desktop
- Ajuste de padding y spacing

## 🚀 Interacciones del Usuario

### Flujo Principal

1. **Inicio**: Pantalla de bienvenida con sugerencias
2. **Selección**: Click en sugerencia o escribir mensaje
3. **Envío**: Enter o click en botón enviar
4. **Carga**: Indicador animado (1.5s simulado)
5. **Respuesta**: Mensaje del AI con acciones
6. **Continuación**: Nuevo mensaje o acciones

### Atajos de Teclado

- `Enter`: Enviar mensaje
- `Shift + Enter`: Nueva línea
- (Próximamente más atajos)

## 🎨 Best Practices Implementadas

1. **Consistencia Visual**: Uso de design tokens
2. **Accesibilidad**: Contraste adecuado, labels semánticos
3. **Performance**: Lazy loading, animaciones optimizadas
4. **UX**: Feedback inmediato, estados de carga claros
5. **Código**: Componentes modulares, TypeScript strict

## 🔮 Próximas Mejoras

- [ ] Markdown rendering en mensajes
- [ ] Code syntax highlighting
- [ ] Arrastrar y soltar archivos
- [ ] Voice-to-text real
- [ ] Streaming de respuestas
- [ ] Historial persistente
- [ ] Temas personalizables
- [ ] Atajos de teclado avanzados
- [ ] Búsqueda global
- [ ] Exportar conversaciones

---

**Versión**: 1.0.0  
**Última Actualización**: Abril 2026
