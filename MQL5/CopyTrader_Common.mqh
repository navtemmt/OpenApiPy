//+------------------------------------------------------------------+
//| CopyTrader_Common.mqh                                            |
//| Common helpers for CopyTrader modules                            |
//+------------------------------------------------------------------+
#ifndef COPYTRADER_COMMON_MQH
#define COPYTRADER_COMMON_MQH

// NOTE:
// Do NOT declare `input` variables here.
// Inputs must be declared once in the main .mq5 EA file, and are visible
// to included headers automatically.

// Helper: escape string for JSON values (quotes + backslashes)
string JsonEscape(const string s)
{
   string out = s;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   return out;
}

#endif // COPYTRADER_COMMON_MQH
