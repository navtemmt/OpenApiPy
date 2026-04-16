#ifndef COPYTRADER_COMMON_MQH
#define COPYTRADER_COMMON_MQH

// NOTE:
// Do NOT declare input variables here.
// Inputs must be declared once in the main .mq4 EA file,
// and are visible to included headers automatically.

// Escape string for safe JSON values
string JsonEscape(const string s)
{
   string out = s;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   return out;
}

#endif // COPYTRADER_COMMON_MQH
