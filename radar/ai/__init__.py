"""Análisis semántico opcional con un proveedor de IA externo.

La aplicación funciona igual sin configurarlo. Solo se envían pares preseleccionados por el
motor léxico; el contenido periodístico se trata como entrada no confiable y el resultado
se valida antes de guardarse. La revisión humana sigue siendo la decisión final.
"""

PROMPT_VERSION = "comparacion-1.1"
