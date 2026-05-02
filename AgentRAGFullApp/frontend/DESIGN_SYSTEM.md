# NexusAI - Sistema de Diseño

## 🎨 Filosofía de Diseño

NexusAI está diseñado con una filosofía **AI-first**, **minimalista** y **moderna**, inspirada en las mejores interfaces de IA como OpenAI, Perplexity y Claude. El sistema prioriza:

1. **Claridad**: Jerarquía visual clara y contenido fácil de escanear
2. **Elegancia**: Uso sutil de gradientes y efectos de profundidad
3. **Fluidez**: Animaciones suaves y transiciones naturales
4. **Accesibilidad**: Contraste adecuado y navegación intuitiva

## 🌈 Paleta de Colores

### Colores Primarios

#### Púrpura (AI Brand Color)

El púrpura fue elegido por sus asociaciones con:
- 🧠 Inteligencia y tecnología
- 🔮 Innovación y futuro
- ✨ Creatividad y posibilidades

**Especificaciones OKLCH:**

```css
/* Modo Claro */
--primary: oklch(0.55 0.22 274);
/* Lightness: 55%, Chroma: 0.22, Hue: 274° */

/* Modo Oscuro */
--primary: oklch(0.65 0.24 274);
/* Lightness: 65%, Chroma: 0.24, Hue: 274° */
```

### ¿Por qué OKLCH?

OKLCH (Oklabs Lightness Chroma Hue) ofrece ventajas sobre RGB/HSL:

1. **Perceptualmente uniforme**: Los cambios en valores producen cambios perceptualmente consistentes
2. **Gamut más amplio**: Acceso a colores más vibrantes
3. **Mejores gradientes**: Transiciones más naturales sin "zonas muertas"
4. **Control preciso**: Separación clara entre luminosidad, saturación y matiz

### Escala de Grises

Todos los grises tienen un sutil tinte púrpura (hue 274°) para mantener coherencia:

```css
/* Modo Claro */
--gray-50:  oklch(0.98 0.005 274)
--gray-100: oklch(0.95 0.005 274)
--gray-200: oklch(0.90 0.005 274)
--gray-500: oklch(0.50 0.01  274)
--gray-900: oklch(0.20 0.01  274)

/* Modo Oscuro */
--gray-100: oklch(0.22 0.015 274)
--gray-200: oklch(0.25 0.02  274)
--gray-500: oklch(0.60 0.02  274)
--gray-900: oklch(0.90 0.008 274)
```

### Colores Semánticos

```css
--success:     oklch(0.65 0.18 160)  /* Verde */
--warning:     oklch(0.70 0.15 85)   /* Amarillo */
--destructive: oklch(0.50 0.20 10)   /* Rojo */
--info:        oklch(0.60 0.18 230)  /* Azul */
```

## 📐 Espaciado

Sistema basado en **4px** como unidad base:

```
0.5  →  2px   (0.125rem)
1    →  4px   (0.25rem)
2    →  8px   (0.5rem)
3    →  12px  (0.75rem)
4    →  16px  (1rem)
6    →  24px  (1.5rem)
8    →  32px  (2rem)
12   →  48px  (3rem)
16   →  64px  (4rem)
```

### Grid System

- **Container máximo**: 1280px (5xl)
- **Chat área máximo**: 768px (3xl) para legibilidad óptima
- **Sidebar width**: 256px (64 × 4px)

## 🔤 Tipografía

### Familia de Fuentes

```css
font-family: 
  -apple-system, 
  BlinkMacSystemFont, 
  "Inter", 
  ui-sans-serif,
  system-ui, 
  "Segoe UI", 
  Roboto, 
  "Helvetica Neue", 
  Arial, 
  sans-serif;
```

**Justificación:** 
- Inter: Excelente legibilidad en pantallas
- System fonts: Performance y familiaridad nativa
- Fallbacks: Amplia compatibilidad

### Escala de Tamaños

```css
text-xs:   0.75rem  (12px)  /* Timestamps, labels */
text-sm:   0.875rem (14px)  /* Secondary text */
text-base: 1rem     (16px)  /* Body text */
text-lg:   1.125rem (18px)  /* Subtitles */
text-xl:   1.25rem  (20px)  /* Section titles */
text-2xl:  1.5rem   (24px)  /* Page titles */
text-3xl:  1.875rem (30px)  /* Hero titles */
```

### Pesos

```css
font-normal:  400  /* Body text */
font-medium:  500  /* Buttons, labels, emphasis */
font-semibold: 600 /* Headings */
```

### Line Heights

```css
leading-tight:   1.25  /* Headings */
leading-normal:  1.5   /* Body (default) */
leading-relaxed: 1.75  /* Long form content */
```

## 🎭 Efectos y Sombras

### Sombras

**Elevación suave** (elementos elevados):
```css
shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.05)
shadow: 0 1px 3px 0 rgb(0 0 0 / 0.1)
shadow-md: 0 4px 6px -1px rgb(0 0 0 / 0.1)
```

**Sombras de color** (CTAs, elementos destacados):
```css
shadow-primary/20: color-mix with primary at 20% opacity
shadow-primary/30: color-mix with primary at 30% opacity
```

### Gradientes

#### Primario (CTAs, elementos destacados)
```css
background: linear-gradient(135deg, 
  oklch(0.65 0.24 274) 0%, 
  oklch(0.6 0.2 290) 100%
);
```

#### Secundario (fondos sutiles)
```css
background: linear-gradient(135deg, 
  oklch(0.6 0.2 260) 0%, 
  oklch(0.55 0.18 200) 100%
);
```

#### Accent (superficies)
```css
background: linear-gradient(135deg, 
  oklch(0.2 0.02 274) 0%, 
  oklch(0.18 0.015 290) 100%
);
```

### Blur Effects

```css
backdrop-blur-sm: blur(4px)   /* Navbars, overlays */
backdrop-blur-md: blur(12px)  /* Modals */
backdrop-blur-lg: blur(16px)  /* Full-screen overlays */
```

## 🔘 Componentes Base

### Botones

#### Sizes
```css
sm:  h-8  px-3  text-xs
md:  h-10 px-4  text-sm  (default)
lg:  h-11 px-8  text-base
```

#### Variants

**Primary** (Gradiente):
```css
background: gradient-primary
color: primary-foreground
hover: opacity 90%
shadow: shadow-lg shadow-primary/20
```

**Outline**:
```css
border: 1px solid border
hover: bg-accent/50, border-primary/40
```

**Ghost**:
```css
hover: bg-accent/50
```

### Inputs

```css
height: 40px (2.5rem)
padding: 8px 12px
border-radius: 8px (0.5rem)
border: 1px solid input
background: input-background
focus: ring-2 ring-ring/20
```

### Cards

```css
background: card
border: 1px solid border
border-radius: 12px (0.75rem)
padding: 16px (1rem) o 24px (1.5rem)
```

## 🎬 Animaciones

### Timing Functions

```css
ease-out:     cubic-bezier(0, 0, 0.2, 1)     /* Entradas */
ease-in:      cubic-bezier(0.4, 0, 1, 1)     /* Salidas */
ease-in-out:  cubic-bezier(0.4, 0, 0.2, 1)   /* Loops */
```

### Duraciones

```css
duration-fast:   150ms  /* Micro-interactions */
duration-base:   200ms  /* Hover states */
duration-medium: 300ms  /* Transitions (default) */
duration-slow:   500ms  /* Page transitions */
```

### Animaciones Comunes

**Fade In:**
```tsx
initial={{ opacity: 0 }}
animate={{ opacity: 1 }}
transition={{ duration: 0.3 }}
```

**Slide Up:**
```tsx
initial={{ opacity: 0, y: 20 }}
animate={{ opacity: 1, y: 0 }}
transition={{ duration: 0.3 }}
```

**Scale:**
```tsx
whileHover={{ scale: 1.05 }}
whileTap={{ scale: 0.95 }}
```

**Rotate (Loading):**
```tsx
animate={{ rotate: 360 }}
transition={{ 
  repeat: Infinity, 
  duration: 2, 
  ease: 'linear' 
}}
```

**Pulse (Indicator):**
```tsx
animate={{ 
  scale: [1, 1.2, 1],
  opacity: [0.5, 1, 0.5]
}}
transition={{ 
  repeat: Infinity, 
  duration: 2 
}}
```

## 📱 Responsive

### Breakpoints

```css
sm:  640px   /* Mobile landscape */
md:  768px   /* Tablet */
lg:  1024px  /* Desktop */
xl:  1280px  /* Large desktop */
2xl: 1536px  /* Wide screens */
```

### Mobile-First Approach

Todos los estilos están diseñados para mobile primero, con ajustes progresivos:

```tsx
<div className="p-4 md:p-6 lg:p-8">
  {/* padding: 16px en mobile, 24px en tablet, 32px en desktop */}
</div>
```

## ♿ Accesibilidad

### Contraste

Todos los pares de colores cumplen **WCAG AA** (4.5:1 para texto normal):

```
✓ primary on white:        7.2:1
✓ foreground on background: 21:1
✓ muted-foreground on bg:  4.8:1
```

### Focus Indicators

```css
focus-visible: ring-2 ring-ring/50
outline: 2px solid transparent
outline-offset: 2px
```

### Semantic HTML

- Uso correcto de `<nav>`, `<main>`, `<aside>`
- Labels para todos los inputs
- Roles ARIA cuando sea necesario

## 🎯 Best Practices

### 1. Consistencia

- Usar tokens de diseño (variables CSS)
- Mantener jerarquía visual clara
- Espaciado consistente (multiplos de 4px)

### 2. Performance

- Minimizar repaints con `transform` y `opacity`
- Usar `will-change` para animaciones complejas
- Lazy loading de componentes pesados

### 3. Mantainability

- Componentes pequeños y reutilizables
- Props tipadas con TypeScript
- Documentación inline

### 4. UX

- Feedback inmediato a acciones
- Estados de carga claros
- Mensajes de error útiles
- Transiciones suaves

---

**Versión**: 1.0.0  
**Última Actualización**: Abril 2026  
**Mantenedor**: Equipo NexusAI
