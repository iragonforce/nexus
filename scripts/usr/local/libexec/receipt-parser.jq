def money:
  if type == "string" and test("^-?[0-9]+\\.[0-9]{2}$")
  then .
  else error("Invalid monetary value")
  end;

def quantity:
  if type == "string" and test("^[0-9]+(\\.[0-9]+)?$")
  then tonumber
  else error("Invalid quantity")
  end;

($raw
 | split("\n")
 | map(rtrimstr("\r") | select(length > 0) | split("|"))
) as $rows
| (
  def validrow:
    . as $r
    | if $r[0] == "ITEM" then ($r | length) == 6
      elif $r[0] == "TAX" or $r[0] == "DISCOUNT"
        then ($r | length) == 3
      else (
        (["MERCHANT","DATE","CURRENCY","SUBTOTAL","TOTAL"]
         | index($r[0])) != null
        and ($r | length) == 2
      )
      end;

  def one($key):
    [$rows[] | select(.[0] == $key)] as $matches
    | if ($matches | length) != 1
      then error("Expected exactly one " + $key)
      else $matches[0][1]
      end;

  if ($rows | length) == 0 or
     ($rows | all(.[]; validrow) | not)
  then error("Invalid receipt record")
  else . end

  | one("CURRENCY") as $currency
  | if $currency != "CAD"
    then error("Unsupported currency")
    else . end

  | {
      schemaVersion: 1,
      source: "apple-cloud",
      merchant: one("MERCHANT"),
      date: one("DATE"),
      currency: $currency,
      subtotal: (
        one("SUBTOTAL")
        | if . == "UNKNOWN" then null else money end
      ),
      total: (one("TOTAL") | money),
      taxes: [
        $rows[] | select(.[0] == "TAX")
        | {name: .[1], amount: (.[2] | money)}
      ],
      discounts: [
        $rows[] | select(.[0] == "DISCOUNT")
        | {name: .[1], amount: (.[2] | money)}
      ],
      items: [
        $rows[] | select(.[0] == "ITEM")
        | {
            name: .[1],
            quantity: (.[2] | quantity),
            unit: .[3],
            unitPrice: (.[4] | money),
            totalPrice: (.[5] | money)
          }
      ]
    }
)
| if .merchant == "UNKNOWN"
     or (.merchant | length) == 0
     or .date == "UNKNOWN"
     or (.items | length) == 0
  then error("Required receipt fields missing")
  else .
  end
