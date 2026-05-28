# Validation template: minimal valid docx-js build()

Este es el mínimo que el modelo debe poder producir si el template solo tiene un título.

```javascript
async function build(data) {
  return new Document({
    creator: 'LexAI',
    title: data.title || 'Documento LexAI',
    sections: [{
      properties: {
        page: {
          size: {
            width: convertInchesToTwip(8.5),
            height: convertInchesToTwip(11),
            orientation: PageOrientation.PORTRAIT,
          },
          margin: {
            top: convertInchesToTwip(1),
            bottom: convertInchesToTwip(1),
            left: convertInchesToTwip(1),
            right: convertInchesToTwip(1),
          },
        },
      },
      children: [
        new Paragraph({
          heading: HeadingLevel.HEADING_1,
          alignment: AlignmentType.CENTER,
          children: [
            new TextRun({
              text: (data.title || 'DOCUMENTO').toUpperCase(),
              bold: true,
              font: 'Arial',
              size: 28,
            }),
          ],
        }),
        new Paragraph({
          alignment: AlignmentType.JUSTIFIED,
          indent: { firstLine: 720 },
          children: [
            new TextRun({
              text: data.body || 'Cuerpo del documento.',
              font: 'Arial',
              size: 22,
            }),
          ],
        }),
      ],
    }],
  });
}
```

Si tu output puede generarse a partir de este molde con sustituciones, está OK.
