// Built-in programs, each chosen to show off a different part of the visualizer.
window.EXAMPLES = [
  {
    name: "Recursion — fibonacci",
    note: "watch the call stack grow and unwind",
    code: `def fib(n):
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)


for i in range(6):
    print(i, "->", fib(i))
`
  },
  {
    name: "Aliasing — two names, one list",
    note: "the arrows show both names pointing at the same object",
    code: `a = [1, 2, 3]
b = a           # b is NOT a copy: same object
c = a[:]        # c IS a copy: new object

b.append(4)
c.append(99)

print("a =", a)
print("b =", b)
print("c =", c)
print("a is b:", a is b)
print("a is c:", a is c)

matrix = [[0] * 3 for _ in range(3)]
matrix[1][1] = 5
print(matrix)
`
  },
  {
    name: "Bubble sort",
    note: "step through the swaps element by element",
    code: `def bubble_sort(values):
    items = list(values)
    n = len(items)
    for i in range(n):
        swapped = False
        for j in range(0, n - i - 1):
            if items[j] > items[j + 1]:
                items[j], items[j + 1] = items[j + 1], items[j]
                swapped = True
        if not swapped:
            break
    return items


data = [5, 1, 4, 2, 8]
print("before:", data)
print("after :", bubble_sort(data))
`
  },
  {
    name: "Classes & objects",
    note: "instances on the heap, self pointing back at them",
    code: `class Account:
    def __init__(self, owner, balance=0):
        self.owner = owner
        self.balance = balance
        self.history = []

    def deposit(self, amount):
        self.balance += amount
        self.history.append(("deposit", amount))
        return self.balance

    def withdraw(self, amount):
        if amount > self.balance:
            raise ValueError("insufficient funds")
        self.balance -= amount
        self.history.append(("withdraw", amount))
        return self.balance


acct = Account("Ada", 100)
acct.deposit(50)
acct.withdraw(30)
print(acct.owner, acct.balance)
print(acct.history)
`
  },
  {
    name: "Linked list",
    note: "objects pointing at objects — follow the chain",
    code: `class Node:
    def __init__(self, value):
        self.value = value
        self.next = None


def build(values):
    head = None
    for value in reversed(values):
        node = Node(value)
        node.next = head
        head = node
    return head


def total(head):
    running = 0
    current = head
    while current is not None:
        running += current.value
        current = current.next
    return running


chain = build([3, 7, 11])
print("sum =", total(chain))
`
  },
  {
    name: "Closures & decorators",
    note: "functions as heap objects, captured state",
    code: `def counter(start=0):
    count = start

    def increment(step=1):
        nonlocal count
        count += step
        return count

    return increment


def logged(func):
    def wrapper(*args):
        result = func(*args)
        print(f"{func.__name__}{args} = {result}")
        return result
    return wrapper


tick = counter(10)
print(tick(), tick(), tick(5))


@logged
def add(a, b):
    return a + b


add(2, 3)
add(10, 20)
`
  },
  {
    name: "Reading input",
    note: "type lines in the Input box before running",
    code: `name = input("Name: ")
count = int(input("How many? "))

for i in range(1, count + 1):
    print(f"{i}. hello {name}")

print("done")
`,
    stdin: "Ada\n3\n"
  },
  {
    name: "Exception & traceback",
    note: "the raising line lights up red",
    code: `def parse_age(text):
    return int(text)


def check(values):
    ages = []
    for value in values:
        ages.append(parse_age(value))
    return ages


print(check(["31", "17"]))
print(check(["44", "oops", "9"]))
`
  }
];
